"""deploy/remote-deploy.sh вызывает drain до rsync и держит маркер до конца (#1588).

Настоящий скрипт гоняется в tmpdir; sudo, rsync, pip, systemctl, curl — шимы в
PATH, которые пишут журнал событий и снимок маркера в момент вызова. Ни root,
ни сети, ни systemd. Проверяется порядок и срок жизни маркера, а не сам drain
(его логику держит tests/test_review_drain.py).
"""

from __future__ import annotations

import gzip
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_review_drain import install_flock

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy/remote-deploy.sh"
DRAIN = ROOT / "deploy/review-drain.sh"
BACKUP = ROOT / "deploy/predeploy-backup.py"

# Шим sudo: разбирает те формы вызова, что есть в remote-deploy.sh, остальное
# падает громко. Снимок маркера пишется в момент rsync / pip / restart.
_SUDO = r"""#!/usr/bin/env bash
snap() {
  local stamp="none" owner="none" backups
  backups="$(ls "$BACKUP_DIR"/predeploy-*.db.gz 2>/dev/null | wc -l | tr -d ' ')"
  if [ -f "$SPOOL/draining" ]; then
    owner="$(sed -n 's/^owner=//p' "$SPOOL/draining")"
    stamp="$(( $(sed -n 's/^expires=//p' "$SPOOL/draining") - $(date +%s) ))"
  fi
  echo "$1 owner=$owner ttl_left=$stamp backups=$backups" >>"$EVENTS"
}
while [ $# -gt 0 ]; do
  case "$1" in
    -n) shift ;;
    -u) shift 2 ;;
    *) break ;;
  esac
done
case "$1" in
  stat) echo svcuser ;;
  rsync) snap rsync ;;
  chown) ;;
  cat)
    # Окружение процесса хаба и EnvironmentFile юнита: путь базы — серверный факт.
    case "$2" in
      /proc/*/environ)
        [ "${PROC_ENV_FAIL:-0}" = 1 ] && exit 1
        printf 'FOO=bar\0'
        [ -z "${PROC_DB:-}" ] || printf 'HAIPLANE_HUB_DB=%s\0' "$PROC_DB"
        ;;
      *) cat "$2" ;;
    esac
    ;;
  /opt/haiplane-hub/venv/bin/pip)
    # Полный argv каждого вызова (#1620): порядок и флаги проверяет тест. Вызов
    # с --require-hashes — установка наборов из lock, второй — приложение.
    echo "pip-argv ${*:2}" >>"$EVENTS"
    deps=0
    case "$*" in *--require-hashes*) deps=1 ;; esac
    snap pip-start
    [ "$deps" = 1 ] && sleep "${PIP_DELAY:-0}"
    snap pip-end
    if [ "$deps" = 1 ] && [ "${PIP_FAIL:-0}" = 1 ]; then
      echo "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE" >&2
      exit 1
    fi
    if [ "$deps" = 0 ] && [ "${PIP_FAIL_APP:-0}" = 1 ]; then
      echo "ERROR: could not build editable" >&2
      exit 1
    fi
    ;;
  systemctl) snap "restart" ;;
  journalctl) ;;
  env) exec "$@" ;;
  *) echo "sudo shim: unexpected $*" >>"$EVENTS"; exit 99 ;;
esac
exit 0
"""

_SYSTEMCTL = r"""#!/usr/bin/env bash
if [ "$1" = show ]; then
  case "$*" in
    *MainPID*) echo "${MAINPID:-0}" ;;
    *EnvironmentFiles*) [ -z "${UNIT_ENVFILE:-}" ] || echo "$UNIT_ENVFILE (ignore_errors=no)" ;;
    *)
      # Environment юнита: чужие переменные (с токеном) и, если задано, каталог очереди.
      out="FOO=bar TOKEN=sekret-token-value"
      [ -z "${UNIT_SPOOL:-}" ] || out="$out HAIPLANE_LOCAL_REVIEW_SPOOL_DIR=$UNIT_SPOOL"
      echo "$out"
      ;;
  esac
  exit 0
fi
echo active
"""

_CURL = r"""#!/usr/bin/env bash
if [ -f "$SPOOL/draining" ]; then
  echo "health ttl_left=$(( $(sed -n 's/^expires=//p' "$SPOOL/draining") - $(date +%s) ))" >>"$EVENTS"
else
  echo "health marker=none" >>"$EVENTS"
fi
printf 200
"""


_HASH = "--hash=sha256:" + "ab" * 32
_RUNTIME_EXPORT = f"fastapi==0.1.0 \\\n    {_HASH}\nhttpx==0.2.0 \\\n    {_HASH}\n"
_BUILD_EXPORT = f"hatchling==1.0.0 \\\n    {_HASH}\neditables==0.6 \\\n    {_HASH}\n"


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        staging = self.home / "haiplane-hub-src-staging" / "deploy"
        staging.mkdir(parents=True)
        (staging / "review-drain.sh").write_text(DRAIN.read_text())
        self.spool = tmp_path / "spool"
        self.spool.mkdir(mode=0o770)
        self.bin = tmp_path / "bin"
        install_flock(self.bin)
        self.events = tmp_path / "events.log"
        self.events.write_text("")
        self.tmp = tmp_path
        (staging / "predeploy-backup.py").write_text(BACKUP.read_text())
        # Экспорты из uv.lock, которые кладёт CI (#1620): без них деплой не идёт.
        self.staging = self.home / "haiplane-hub-src-staging"
        self.runtime_export = self.staging / "requirements.runtime.txt"
        self.build_export = self.staging / "requirements.build.txt"
        self.lock = self.staging / "uv.lock"
        self.runtime_export.write_text(_RUNTIME_EXPORT)
        self.build_export.write_text(_BUILD_EXPORT)
        self.lock.write_text("version = 1\nrevision = 3\n")
        # Настоящая база в WAL с ОТКРЫТЫМ соединением: закоммиченные строки лежат
        # в -wal и в основной файл ещё не перенесены.
        self.db = tmp_path / "hubdata" / "hub.db"
        self.db.parent.mkdir()
        self.backups = tmp_path / "hubdata" / "backups"
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        self.conn.executemany(
            "INSERT INTO tasks (title) VALUES (?)", [(f"task-{i}",) for i in range(50)]
        )
        self.conn.commit()
        for name, body in (("sudo", _SUDO), ("systemctl", _SYSTEMCTL), ("curl", _CURL)):
            shim = self.bin / name
            shim.write_text(body)
            shim.chmod(0o755)

    def env(self, **over: str) -> dict[str, str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "SPOOL": str(self.spool),
            "EVENTS": str(self.events),
            "HAIPLANE_LOCAL_REVIEW_SPOOL_DIR": str(self.spool),
            "DRAIN_TTL_SECONDS": "2",
            "DRAIN_RENEW_SECONDS": "0.4",
            "DRAIN_POLL_SECONDS": "0.1",
            "DRAIN_BUDGET_SECONDS": "6",
            "HEALTH_BUDGET_SECONDS": "5",
            "MAINPID": "4242",
            "PROC_DB": str(self.db),
            "BACKUP_DIR": str(self.backups),
            "BACKUP_PYTHON": sys.executable,
        }
        env.update(over)
        return env

    def start(self, **over: str) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", str(DEPLOY)],
            env=self.env(**over),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def lines(self) -> list[str]:
        return self.events.read_text().splitlines()

    def event(self, name: str) -> dict[str, str]:
        for line in self.lines():
            head, *rest = line.split()
            if head == name:
                return dict(p.split("=", 1) for p in rest)
        raise AssertionError(f"нет события {name}: {self.lines()}")

    def marker_file(self) -> Path:
        return self.spool / "draining"


@pytest.fixture
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    yield sandbox
    sandbox.conn.close()


def _job(spool: Path, name: str = "job-0123456789abcdef") -> Path:
    jobdir = spool / name
    jobdir.mkdir(mode=0o770)
    (jobdir / "job.json").write_text("{}")
    return jobdir


def test_deploy_drains_before_rsync_and_releases_own_marker(box) -> None:
    """AC-5: drain до rsync, повторная проверка с остатком срока, маркер живёт
    весь долгий pip и до конца health, снимается при успехе, ошибке и сигнале."""
    # --- успех: идущее задание завершается во время drain, pip дольше TTL ---
    job = _job(box.spool)
    proc = box.start(PIP_DELAY="3")
    time.sleep(1.0)
    assert proc.poll() is None, "deploy не дождался идущего задания"
    assert not [line for line in box.lines() if line.startswith("rsync")], (
        "rsync начался, пока задание идёт"
    )
    (job / "result.json").write_text("{}")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "deploy ok" in out

    rsync = box.event("rsync")
    assert rsync["owner"].startswith("deploy-"), "маркер поставлен ДО rsync"
    assert int(rsync["ttl_left"]) > 0
    assert out.index("drain ok (acquire") < out.index("service=active")
    # Долгий pip (3 с) дольше TTL (2 с): маркер продлён независимо от цикла ожидания.
    assert int(box.event("pip-end")["ttl_left"]) > 0, "маркер просрочился за pip"
    assert int(box.event("restart")["ttl_left"]) > 0
    assert int(box.event("health")["ttl_left"]) > 0, "маркер снят до конца health"
    # Повторная проверка — до restart и с остатком ОДНОГО срока, не со свежим.
    recheck = [x for x in out.splitlines() if "перед restart" in x]
    assert recheck and recheck[0].startswith("drain ok")
    left = int(recheck[0].split("осталось срока ")[1].split(" ")[0])
    assert left <= 6 - 3, f"повторная проверка получила новый срок: {left}"
    assert out.index("перед restart") < out.index("service=active")
    assert not box.marker_file().exists(), "после успеха маркер снят"

    # --- ошибка после drain: pip падает, свой маркер всё равно снят ---
    box.events.write_text("")
    done = box.start(PIP_FAIL="1")
    out, _ = done.communicate(timeout=60)
    assert done.returncode != 0
    assert "drain ok" in out
    assert not box.marker_file().exists(), "ошибка деплоя оставила маркер"

    # --- чужой маркер: деплой его не перезаписывает и не снимает ---
    foreign = f"owner=deploy-other\nexpires={int(time.time()) + 600}\n"
    box.marker_file().write_text(foreign)
    done = box.start(DRAIN_BUDGET_SECONDS="1")
    out, _ = done.communicate(timeout=60)
    assert done.returncode == 0, out
    assert "drain degraded" in out and "deploy ok" in out, "drain не валит деплой"
    assert box.marker_file().read_text() == foreign
    box.marker_file().unlink()

    # --- сигнал во время pip: свой маркер снят, продлитель не остался жить ---
    box.events.write_text("")
    sig = box.start(PIP_DELAY="3")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(
        x.startswith("pip-start") for x in box.lines()
    ):
        time.sleep(0.05)
    assert any(x.startswith("pip-start") for x in box.lines())
    sig.send_signal(signal.SIGTERM)
    out, _ = sig.communicate(timeout=60)
    assert sig.returncode == 143, (sig.returncode, out)
    assert not box.marker_file().exists(), "сигнал оставил маркер"
    time.sleep(1.0)  # продлитель, если бы жил, поставил бы маркер заново
    assert not box.marker_file().exists(), "продлитель пережил deploy"


def test_a_missing_drain_script_is_degraded_not_a_failed_deploy(box) -> None:
    (box.home / "haiplane-hub-src-staging/deploy/review-drain.sh").unlink()
    proc = box.start()
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "drain degraded" in out and "deploy ok" in out
    assert box.event("rsync")["owner"] == "none"


def test_the_drain_runs_as_the_hub_user_with_the_deploy_state(box) -> None:
    """sudo -u <пользователь хаба> env ... bash -s: конкретный вызов из CD.md."""
    proc = box.start()
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    text = DEPLOY.read_text()
    assert 'sudo -n -u "$SERVICE_USER" env' in text
    assert 'bash -c "$(cat "$REVIEW_DRAIN_SCRIPT")" review-drain' in text
    assert text.index("drain_step acquire") < text.index("sudo rsync")
    assert text.index("drain_step recheck") < text.index("sudo systemctl restart")


def test_the_deploy_job_has_its_own_limit_and_keeps_the_ssh_channel_alive() -> None:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["deploy"]
    assert job["timeout-minutes"] == 45
    rollout = next(s for s in job["steps"] if s.get("id") == "rollout")
    assert "ServerAliveInterval=30" in rollout["run"]
    assert "remote-deploy.sh" in rollout["run"]


def test_the_spool_comes_from_the_hub_unit_and_nothing_else_of_it_is_kept(box) -> None:
    """Каталог очереди — серверный факт: берётся из Environment юнита хаба, а
    остальное его окружение (токен) не печатается и не уходит в drain."""
    proc = box.start(HAIPLANE_LOCAL_REVIEW_SPOOL_DIR="", UNIT_SPOOL=str(box.spool))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "drain ok" in out
    assert box.event("rsync")["owner"].startswith("deploy-"), "drain шёл по spool юнита"
    assert "sekret-token-value" not in out
    assert "sekret-token-value" not in "".join(box.lines())


def test_no_spool_anywhere_is_named_degraded_and_the_deploy_goes_on(box) -> None:
    proc = box.start(HAIPLANE_LOCAL_REVIEW_SPOOL_DIR="", UNIT_SPOOL="")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "drain degraded (каталог очереди не задан" in out
    assert "deploy ok" in out
    assert box.event("rsync")["owner"] == "none"
    assert "sekret-token-value" not in out


def test_a_killed_deploy_frees_its_marker_through_the_liveness_channel(box) -> None:
    """SIGKILL: ни trap, ни cleanup не работают. Продлитель (другой uid, kill
    ему недоступен) узнаёт о смерти деплоя по EOF трубы и снимает свой маркер."""
    proc = box.start(PIP_DELAY="25")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(
        x.startswith("pip-start") for x in box.lines()
    ):
        time.sleep(0.05)
    assert box.marker_file().exists()
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 8  # pip-шим ещё спит (25 с): fd канала ему не отданы
    while time.monotonic() < deadline and box.marker_file().exists():
        time.sleep(0.1)
    assert not box.marker_file().exists(), "маркер убитого деплоя остался"


# ---- #1590: проверенный снимок базы до замены кода ---------------------------


def _predeploy(box: Sandbox) -> list[Path]:
    return sorted(box.backups.glob("predeploy-*.db.gz"))


def _leftovers(box: Sandbox) -> list[str]:
    if not box.backups.exists():
        return []
    return sorted(
        p.name for p in box.backups.iterdir() if p.name.startswith(".predeploy-")
    )


def _restored(tmp_path: Path, archive: Path) -> sqlite3.Connection:
    raw = tmp_path / "restored.db"
    raw.write_bytes(gzip.decompress(archive.read_bytes()))
    return sqlite3.connect(raw)


def test_deploy_takes_a_verified_backup_before_restart(box, tmp_path) -> None:
    """AC-1 (#1590): снимок до rsync, 0600 в каталоге 0700, закоммиченное из WAL
    внутри, integrity_check, порядок drain -> снимок -> rsync -> restart,
    очистка только своих файлов, sha в имени."""
    assert (box.db.parent / "hub.db-wal").stat().st_size > 0, (
        "данные должны лежать в WAL"
    )
    box.backups.mkdir(mode=0o755)  # каталог уже есть, но шире нужного
    stale = [f"predeploy-2026010{i}T000000Z-aaaaaaa.db.gz" for i in range(1, 9)]
    for n in range(4):
        stale.append(f"predeploy-2026020{n}T000000Z-bbbbbbb.db.gz")
    for name in stale:
        (box.backups / name).write_bytes(b"old")
    nightly = box.backups / "hub-nightly-20260101.db.gz"
    nightly.write_bytes(b"nightly")

    proc = box.start(DEPLOY_SHA="0123456cafe")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out

    line = next(x for x in out.splitlines() if x.startswith("backup: "))
    assert line.startswith("backup: ok ") and "integrity=ok" in line, line
    assert " size=" in line and " seconds=" in line
    # порядок в журнале: drain ok -> backup -> service=active; и в событиях шимов
    assert out.index("drain ok") < out.index("backup: ok") < out.index("service=active")
    rsync, restart = box.event("rsync"), box.event("restart")
    assert rsync["owner"].startswith("deploy-"), "снимок идёт под маркером drain"
    assert int(rsync["backups"]) == 10 and int(restart["backups"]) == 10, (
        "к rsync снимок уже опубликован, а старые сверх N=10 удалены"
    )
    assert [
        e.split()[0] for e in box.lines() if e.split()[0] in ("rsync", "restart")
    ] == [
        "rsync",
        "restart",
    ]

    files = _predeploy(box)
    assert len(files) == 10, [f.name for f in _predeploy(box)]
    assert nightly.read_bytes() == b"nightly", "ночные не трогаем"
    fresh = max(files, key=lambda f: f.stat().st_mtime)
    assert fresh.name.endswith("-0123456.db.gz"), fresh.name
    assert oct(fresh.stat().st_mode & 0o777) == "0o600"
    assert oct(box.backups.stat().st_mode & 0o777) == "0o700"
    assert _leftovers(box) == [], "временные файлы остались"
    assert line.split()[2] == str(fresh)

    copy = _restored(tmp_path, fresh)
    assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert copy.execute("SELECT COUNT(*) FROM tasks").fetchone() == (50,), (
        "закоммиченное, но не перенесённое из WAL, потеряно"
    )
    copy.close()
    assert not box.marker_file().exists()


def test_the_name_is_manual_without_a_sha_and_unique_on_a_repeat(box) -> None:
    for _ in range(2):
        proc = box.start()
        out, _ = proc.communicate(timeout=60)
        assert proc.returncode == 0, out
    names = [f.name for f in _predeploy(box)]
    assert len(names) == len(set(names)) == 2, names
    assert all("-manual" in n for n in names), names


def test_the_sha_is_read_from_the_staging_file_when_no_variable(box) -> None:
    """Форсированная команда ключа CI не пускает переменных: sha едет файлом."""
    (box.home / "haiplane-hub-src-staging/.deploy-sha").write_text("ABCDEF0123456789\n")
    proc = box.start()
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert _predeploy(box)[0].name.endswith("-abcdef0.db.gz")


def _no_restart(box: Sandbox) -> None:
    kinds = [e.split()[0] for e in box.lines()]
    assert "rsync" not in kinds and "restart" not in kinds, box.lines()
    assert not box.marker_file().exists(), "drain-маркер остался"
    assert _predeploy(box) == [] and _leftovers(box) == []


def test_failed_backup_blocks_the_restart(box, tmp_path) -> None:
    """AC-2 (#1590): неудача снимка и сигнал -> ни rsync, ни restart, код ошибки,
    backup: failed, нет частичных файлов, маркер снят; неопределённый путь —
    failed, а не skipped; обход и «базы нет» — skipped, деплой идёт."""
    # --- каталог недоступен ---
    blocker = tmp_path / "blocker"
    blocker.write_text("файл, а не каталог")
    proc = box.start(BACKUP_DIR=str(blocker / "sub"))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0 and "backup: failed (" in out, out
    assert "deploy ok" not in out
    _no_restart(box)

    # --- каталог чужой: владелец не пользователь хаба (шим: root-подобный uid не
    # воспроизвести; проверяем через симлинк на каталог — он не каталог по lstat) ---
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    proc = box.start(BACKUP_DIR=str(link))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0 and "backup: failed (" in out, out
    assert list(real.iterdir()) == []
    _no_restart(box)

    # --- повреждённая копия: байт данных строки затёрт (таблица и индекс
    # расходятся). Backup API копирует страницы не глядя, integrity_check копии
    # это видит ---
    bad = tmp_path / "bad" / "hub.db"
    bad.parent.mkdir()
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    con.execute("CREATE INDEX t_b ON t (b)")
    con.executemany(
        "INSERT INTO t VALUES (?, ?)", [(i, f"zzz{i:03d}") for i in range(30)]
    )
    con.commit()
    con.close()
    raw = bad.read_bytes()
    assert raw.count(b"zzz007") == 2
    bad.write_bytes(raw.replace(b"zzz007", b"zzz9X7", 1))
    # Очистка идёт только после успешной публикации: при провале прежние снимки целы.
    box.backups.mkdir(exist_ok=True, mode=0o700)
    seeded = {f"predeploy-2026010{i}T000000Z-aaaaaaa.db.gz" for i in range(1, 10)} | {
        f"predeploy-2026020{i}T000000Z-bbbbbbb.db.gz" for i in range(1, 5)
    }
    for name in seeded:
        (box.backups / name).write_bytes(b"old")
    proc = box.start(PROC_DB=str(bad))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0, out
    assert "backup: failed (" in out and "integrity" in out, out
    assert {f.name for f in _predeploy(box)} == seeded, "провал снимка тронул прежние"
    for name in seeded:
        (box.backups / name).unlink()
    _no_restart(box)

    # --- путь базы не определён: failed, а не «базы нет» ---
    for over in ({"PROC_DB": ""}, {"PROC_ENV_FAIL": "1", "UNIT_ENVFILE": ""}):
        proc = box.start(**over)
        out, _ = proc.communicate(timeout=60)
        assert proc.returncode != 0, out
        assert "backup: failed (" in out and "backup: skipped" not in out, out
        _no_restart(box)

    # --- сигнал во время снимка ---
    slow = tmp_path / "slowpy"
    slow.mkdir()
    (slow / "sitecustomize.py").write_text(
        "import sqlite3, time\n"
        "class _Slow(sqlite3.Connection):\n"
        "    def backup(self, *a, **k):\n"
        "        time.sleep(30)\n"
        "        return super().backup(*a, **k)\n"
        "_orig = sqlite3.connect\n"
        "def connect(*a, **k):\n"
        "    k.setdefault('factory', _Slow)\n"
        "    return _orig(*a, **k)\n"
        "sqlite3.connect = connect\n"
    )
    sig = box.start(PYTHONPATH=str(slow))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not _leftovers(box):
        time.sleep(0.05)
    assert _leftovers(box), "снимок не начался"
    sig.send_signal(signal.SIGTERM)
    out, _ = sig.communicate(timeout=60)
    assert sig.returncode == 143, (sig.returncode, out)
    _no_restart(box)

    # --- обход: снимка нет, деплой идёт ---
    proc = box.start(DEPLOY_SKIP_BACKUP="1", PROC_DB="", MAINPID="0")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "backup: skipped (обход DEPLOY_SKIP_BACKUP=1)" in out and "deploy ok" in out
    assert _predeploy(box) == []

    # --- путь определён, файла нет (первая установка): skipped, деплой идёт ---
    box.events.write_text("")
    proc = box.start(PROC_DB=str(tmp_path / "nowhere" / "hub.db"))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "backup: skipped (базы нет" in out and "deploy ok" in out
    assert "rsync" in [e.split()[0] for e in box.lines()]


def test_the_database_path_comes_from_the_unit_when_no_process_runs(
    box, tmp_path
) -> None:
    """Процесса нет: путь берётся из EnvironmentFile юнита (на проде — так)."""
    envfile = tmp_path / "hub.env"
    envfile.write_text(f'OTHER=1\nHAIPLANE_HUB_DB="{box.db}"\n')
    proc = box.start(MAINPID="0", PROC_DB="", UNIT_ENVFILE=str(envfile))
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "backup: ok " in out and len(_predeploy(box)) == 1


# ---- #1620: dependencies come from the lock exports, wheels only, by hash -----


def _pip_calls(box) -> list[list[str]]:
    return [line.split()[1:] for line in box.lines() if line.startswith("pip-argv ")]


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_deploy_installs_dependencies_strictly_from_the_lock(box) -> None:
    """AC-1 (#1620): backup -> rsync -> pip по хешам -> pip приложения -> recheck
    -> restart; колёса, хеши, --no-deps; строка deps с sha256 экспортов и lock."""
    proc = box.start()
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    calls = _pip_calls(box)
    assert len(calls) == 2, calls
    deps, app = calls
    # Наборы: build и runtime одной командой, строго по хешам, только колёса.
    assert deps[0] == "install"
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps"):
        assert flag in deps, (flag, deps)
    files = [deps[i + 1] for i, a in enumerate(deps) if a == "-r"]
    assert [Path(f).name for f in files] == [
        "requirements.build.txt",
        "requirements.runtime.txt",
    ], deps
    # Приложение: без разрешения зависимостей и без изолированной сборки.
    assert app[0] == "install"
    assert "--no-deps" in app and "--no-build-isolation" in app, app
    assert "-e" in app and app[app.index("-e") + 1] == "/opt/haiplane-hub/src", app
    assert "-r" not in app

    # Порядок: бэкап (в снимке rsync уже есть копия) -> rsync -> pip -> restart.
    heads = [x.split()[0] for x in box.lines() if not x.startswith("health")]
    assert heads.index("rsync") < heads.index("pip-argv"), heads
    assert heads.index("pip-argv") < heads.index("restart"), heads
    assert box.event("rsync")["backups"] == "1", "rsync раньше бэкапа"
    assert (
        out.index("backup: ok")
        < out.index("перед restart")
        < out.index("service=active")
    )
    assert out.index("deps: ") < out.index("перед restart"), "recheck раньше установки"

    line = next(x for x in out.splitlines() if x.startswith("deps: "))
    assert "4 пакетов (runtime 2, build 2)" in line, line
    assert f"runtime={_sha(box.runtime_export)}" in line, line
    assert f"build={_sha(box.build_export)}" in line, line
    assert f"uv.lock sha256 {_sha(box.lock)}" in line, line


def test_a_missing_or_broken_lock_export_stops_before_restart(box) -> None:
    """AC-2 (#1620): нет экспорта -> отказ ДО rsync; хеш не сошёлся или приложение
    не встало -> отказ до restart с причиной; везде restart не вызван, маркер
    снят, pip с разрешением зависимостей не вызывался."""

    def failed(env: dict[str, str] | None = None) -> str:
        box.events.write_text("")
        proc = box.start(**(env or {}))
        out, _ = proc.communicate(timeout=60)
        assert proc.returncode != 0, out
        assert "deploy ok" not in out
        assert not [x for x in box.lines() if x.startswith("restart")], box.lines()
        assert not box.marker_file().exists(), "отказ оставил маркер"
        for call in _pip_calls(box):
            assert "--no-deps" in call, f"разрешение зависимостей: {call}"
        return out

    # (а) нет любого из двух экспортов (и нет lock) - до любых изменений.
    for victim in (box.runtime_export, box.build_export, box.lock):
        saved = victim.read_text()
        victim.unlink()
        out = failed()
        assert victim.name in out, out
        assert not [x for x in box.lines() if x.startswith("rsync")], box.lines()
        assert _pip_calls(box) == [], "pip вызван при отказе предпроверки"
        assert "backup:" not in out, "предпроверка должна идти до любых действий"
        victim.write_text(saved)
    # Пустой и нехешированный экспорт - тот же отказ.
    for body in ("", "fastapi==0.1.0\n"):
        box.runtime_export.write_text(body)
        out = failed()
        assert "requirements.runtime.txt" in out, out
        assert not [x for x in box.lines() if x.startswith("rsync")], box.lines()
    box.runtime_export.write_text(_RUNTIME_EXPORT)

    # (б) pip падает на хеше: приложение не ставится, причина в логе.
    out = failed({"PIP_FAIL": "1"})
    assert "DO NOT MATCH THE HASHES" in out, out
    assert "deps: failed" in out and "restart not called" in out, out
    assert len(_pip_calls(box)) == 1, "приложение поставлено после отказа хеша"
    assert "пакетов (runtime" not in out

    # (в) падает установка приложения.
    out = failed({"PIP_FAIL_APP": "1"})
    assert "could not build editable" in out, out
    assert "deps: failed" in out and "restart not called" in out, out
    assert "пакетов (runtime" not in out
