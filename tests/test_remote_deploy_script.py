"""deploy/remote-deploy.sh вызывает drain до rsync и держит маркер до конца (#1588).

Настоящий скрипт гоняется в tmpdir; sudo, rsync, pip, systemctl, curl — шимы в
PATH, которые пишут журнал событий и снимок маркера в момент вызова. Ни root,
ни сети, ни systemd. Проверяется порядок и срок жизни маркера, а не сам drain
(его логику держит tests/test_review_drain.py).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_review_drain import install_flock

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy/remote-deploy.sh"
DRAIN = ROOT / "deploy/review-drain.sh"

# Шим sudo: разбирает те формы вызова, что есть в remote-deploy.sh, остальное
# падает громко. Снимок маркера пишется в момент rsync / pip / restart.
_SUDO = r"""#!/usr/bin/env bash
snap() {
  local stamp="none" owner="none"
  if [ -f "$SPOOL/draining" ]; then
    owner="$(sed -n 's/^owner=//p' "$SPOOL/draining")"
    stamp="$(( $(sed -n 's/^expires=//p' "$SPOOL/draining") - $(date +%s) ))"
  fi
  echo "$1 owner=$owner ttl_left=$stamp" >>"$EVENTS"
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
  /opt/haiplane-hub/venv/bin/pip)
    snap pip-start
    sleep "${PIP_DELAY:-0}"
    snap pip-end
    [ "${PIP_FAIL:-0}" = 1 ] && exit 1
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
  # Environment юнита: чужие переменные (с токеном) и, если задано, каталог очереди.
  out="FOO=bar TOKEN=sekret-token-value"
  [ -z "${UNIT_SPOOL:-}" ] || out="$out HAIPLANE_LOCAL_REVIEW_SPOOL_DIR=$UNIT_SPOOL"
  echo "$out"
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
def box(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


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
