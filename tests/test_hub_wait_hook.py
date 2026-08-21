"""The Stop-hook's wait file is shared by every session in the directory (#772).

Three sessions worked in this checkout on 2026-08-20 and woke each other four
times, because the hook read every wait in the file regardless of who was
waiting for it — and a session rewriting the file dropped the others' waits,
which is how one task ended up delivered by two agents at once.

The fix is one optional field. These tests pin the part that is easy to get
wrong: a wait WITHOUT an owner must keep behaving exactly as before, or the
change would only work once every session had already adopted it.

Later the same day the file itself was split per session: the owner field was
not enough, because in the background asyncRewake run the Stop payload arrives
empty, so no session ever resolved and every wait counted as shared. The tests
below therefore point the hook at a temporary directory rather than at a single
file, and the new ones pin what the split added — own file, own lock, and the
fallback that watches everything when the session cannot be named.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "hub_wait_hook.py"
)

MINE = "session-mine"
THEIRS = "session-theirs"


def _load(tmp_path: Path, monkeypatch=None):
    spec = importlib.util.spec_from_file_location("hub_wait_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CLAUDE_DIR = tmp_path
    module.SHARED_STATE_FILE = tmp_path / "hub-wait.json"
    module.MAX_WAIT_SEC = 5
    # Окружение разработчика несёт CLAUDE_CODE_SESSION_ID, и без этого тест
    # «сессия не определена» проходил бы только в CI — то есть проверял бы не
    # то, что написано в его названии.
    if monkeypatch is not None:
        for key in ("CLAUDE_SESSION_ID", "CLAUDE_SESSION", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
    return module


def _shared(module):
    return module.SHARED_STATE_FILE


def _wire_hub(module, monkeypatch, tasks: dict[int, dict]):
    """A hub where every named task already differs from its baseline."""
    monkeypatch.setattr(module, "hub_config", lambda: ("https://hub.example", "t"))
    monkeypatch.setattr(module, "feed_tail_cursor", lambda *a, **k: None)
    monkeypatch.setattr(module, "fetch_events", lambda *a, **k: None)
    monkeypatch.setattr(module, "fetch_task", lambda base, auth, tid: tasks.get(tid))


def _run(module, monkeypatch, payload: dict) -> int:
    monkeypatch.setattr(module.sys, "stdin", _Stdin(json.dumps(payload)))
    return module.main()


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def test_session_id_comes_from_the_payload_or_the_transcript_name(tmp_path):
    module = _load(tmp_path)
    assert module.session_id_from(json.dumps({"session_id": MINE})) == MINE
    assert (
        module.session_id_from(
            json.dumps({"transcript_path": f"/x/projects/p/{MINE}.jsonl"})
        )
        == MINE
    ), "the transcript is named after the session — the same id from the other side"
    assert module.session_id_from("not json") == ""
    assert module.session_id_from(json.dumps({})) == ""


def test_unowned_waits_and_unknown_sessions_behave_as_before(tmp_path):
    """AC-3: the compatibility that makes a partial rollout safe."""
    module = _load(tmp_path)
    assert module.is_own_wait({"task_id": 1}, MINE), "no owner means shared"
    assert module.is_own_wait({"task_id": 1, "owner": THEIRS}, ""), (
        "an undetermined session must not silence anything"
    )
    assert module.is_own_wait({"task_id": 1, "owner": MINE}, MINE)
    assert not module.is_own_wait({"task_id": 1, "owner": THEIRS}, MINE)


def test_foreign_waits_neither_wake_nor_vanish(tmp_path, monkeypatch, capsys):
    """AC-1 and AC-2 together: only my event, and their wait survives it."""
    module = _load(tmp_path, monkeypatch)
    _shared(module).write_text(
        json.dumps(
            {
                "waits": [
                    {
                        "task_id": 761,
                        "owner": THEIRS,
                        "reason": "их ожидание",
                        "baseline": {"status": "review"},
                        "fired_at": "2026-08-20T19:00:00Z",
                        "refire_count": 2,
                    },
                    {
                        "task_id": 762,
                        "owner": MINE,
                        "reason": "моё ожидание",
                        "baseline": {"status": "review"},
                    },
                ]
            },
            ensure_ascii=False,
        )
    )
    _wire_hub(
        module,
        monkeypatch,
        {
            761: {"id": 761, "title": "их задача", "status": "completed"},
            762: {"id": 762, "title": "моя задача", "status": "completed"},
        },
    )

    rc = _run(module, monkeypatch, {"session_id": MINE})

    assert rc == 2, "my own wait must still wake me"
    out = capsys.readouterr().out
    assert "#762" in out and "моя задача" in out
    assert "#761" not in out and "их задача" not in out, (
        "someone else's event must not spend my turn"
    )
    assert f"Сессия: {MINE}" in out, (
        "the resolved id belongs in the message: it is how a mismatch becomes "
        "visible instead of silently switching wake-ups off"
    )

    left = json.loads(_shared(module).read_text())["waits"]
    theirs = [w for w in left if w["task_id"] == 761]
    assert theirs == [
        {
            "task_id": 761,
            "owner": THEIRS,
            "reason": "их ожидание",
            "baseline": {"status": "review"},
            "fired_at": "2026-08-20T19:00:00Z",
            "refire_count": 2,
        }
    ], "their wait must come back byte for byte, counters included"


def test_a_file_of_only_foreign_waits_is_not_our_business(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    _shared(module).write_text(
        json.dumps(
            {"waits": [{"task_id": 761, "owner": THEIRS, "baseline": {"status": "x"}}]}
        )
    )
    called: list[int] = []

    def _boom(base, auth, tid):
        called.append(tid)
        raise AssertionError("a foreign wait must not even be polled")

    monkeypatch.setattr(module, "hub_config", lambda: ("https://hub.example", "t"))
    monkeypatch.setattr(module, "fetch_task", _boom)

    assert _run(module, monkeypatch, {"session_id": MINE}) == 0
    assert not called
    assert _shared(module).exists(), "and it must still be there for its owner"


@pytest.mark.parametrize("payload", [{}, {"session_id": ""}])
def test_without_a_session_id_every_wait_is_ours(
    tmp_path, monkeypatch, capsys, payload
):
    """AC-3 again, from the hook's side: today's behaviour, unchanged."""
    module = _load(tmp_path, monkeypatch)
    _shared(module).write_text(
        json.dumps(
            {
                "waits": [
                    {
                        "task_id": 761,
                        "owner": THEIRS,
                        "reason": "чужое",
                        "baseline": {"status": "review"},
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    _wire_hub(module, monkeypatch, {761: {"id": 761, "title": "t", "status": "done"}})

    assert _run(module, monkeypatch, payload) == 2
    assert "#761" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Файл и лок на сессию: owner был правильной идеей, которая не работала.
# ---------------------------------------------------------------------------


def _wait(task_id: int, owner: str | None = None, reason: str = "r") -> dict:
    w = {"task_id": task_id, "reason": reason, "baseline": {"status": "review"}}
    if owner:
        w["owner"] = owner
    return w


def _write(path: Path, *waits: dict) -> None:
    path.write_text(json.dumps({"waits": list(waits)}, ensure_ascii=False))


def test_each_session_reads_only_its_own_file(tmp_path, monkeypatch, capsys):
    """Чужой файл не опрашивается и не переписывается — даже байтом."""
    module = _load(tmp_path, monkeypatch)
    _write(tmp_path / f"hub-wait.{MINE}.json", _wait(1, reason="моё"))
    _write(tmp_path / f"hub-wait.{THEIRS}.json", _wait(2, reason="чужое"))
    _wire_hub(
        module,
        monkeypatch,
        {
            1: {"id": 1, "title": "моя задача", "status": "completed"},
            2: {"id": 2, "title": "чужая задача", "status": "completed"},
        },
    )
    before = (tmp_path / f"hub-wait.{THEIRS}.json").read_text()

    assert _run(module, monkeypatch, {"session_id": MINE}) == 2
    out = capsys.readouterr().out
    assert "#1" in out and "#2" not in out
    assert (tmp_path / f"hub-wait.{THEIRS}.json").read_text() == before, (
        "чужой файл не трогаем вообще: ни счётчиков, ни форматирования"
    )
    assert f"hub-wait.{MINE}.json" in out, (
        "сообщение называет файл, который агент должен разгрести"
    )


def test_the_session_id_can_come_from_the_environment(tmp_path, monkeypatch, capsys):
    """Главная причина, по которой owner простаивал: пустой payload в фоне.

    Stop-хук с asyncRewake запускается фоном, и stdin приходил пустым — за
    вечер 20.08 ни одно пробуждение не содержало строки «Сессия», то есть
    разделение владельцев не срабатывало ни разу.
    """
    module = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", MINE)
    _write(tmp_path / f"hub-wait.{MINE}.json", _wait(1))
    _write(tmp_path / f"hub-wait.{THEIRS}.json", _wait(2))
    _wire_hub(
        module,
        monkeypatch,
        {
            1: {"id": 1, "title": "моя", "status": "completed"},
            2: {"id": 2, "title": "чужая", "status": "completed"},
        },
    )

    assert _run(module, monkeypatch, {}) == 2, "payload пуст — окружение назвало сессию"
    out = capsys.readouterr().out
    assert "#1" in out and "#2" not in out
    assert f"Сессия: {MINE}" in out


def test_an_unnamed_session_watches_everything_and_says_so(
    tmp_path, monkeypatch, capsys
):
    """Неопределённость не выключает пробуждения — и не прячется."""
    module = _load(tmp_path, monkeypatch)
    _write(tmp_path / f"hub-wait.{THEIRS}.json", _wait(2, owner=THEIRS))
    _wire_hub(module, monkeypatch, {2: {"id": 2, "title": "чужая", "status": "done"}})

    assert _run(module, monkeypatch, {}) == 2
    out = capsys.readouterr().out
    assert "#2" in out
    assert "session_id не определён" in out, (
        "иначе разбор лишнего пробуждения начинается с догадок"
    )
    assert (tmp_path / "hub-wait-unidentified.json").exists(), (
        "и остаётся след на диске: отсутствие файла — сигнал, что всё в порядке"
    )


def test_a_foreign_lock_does_not_silence_this_session(tmp_path, monkeypatch):
    """Общий лок означал, что вторая сессия оставалась без поллера вовсе."""
    import os

    module = _load(tmp_path, monkeypatch)
    (tmp_path / f"hub-wait.{THEIRS}.lock").write_text(str(os.getpid()))  # живой чужой
    _write(tmp_path / f"hub-wait.{MINE}.json", _wait(1))
    _wire_hub(module, monkeypatch, {1: {"id": 1, "title": "моя", "status": "done"}})

    assert _run(module, monkeypatch, {"session_id": MINE}) == 2
    assert not (tmp_path / f"hub-wait.{MINE}.lock").exists(), "свой лок снят за собой"


def test_a_shared_file_keeps_working_during_the_rollout(tmp_path, monkeypatch, capsys):
    """Сессия, ещё пишущая в общий файл, не остаётся без пробуждений."""
    module = _load(tmp_path, monkeypatch)
    _write(_shared(module), _wait(3, owner=MINE), _wait(4, owner=THEIRS))
    _wire_hub(
        module,
        monkeypatch,
        {
            3: {"id": 3, "title": "моя в общем", "status": "completed"},
            4: {"id": 4, "title": "чужая в общем", "status": "completed"},
        },
    )

    assert _run(module, monkeypatch, {"session_id": MINE}) == 2
    out = capsys.readouterr().out
    assert "#3" in out and "#4" not in out
    left = [w["task_id"] for w in json.loads(_shared(module).read_text())["waits"]]
    assert 4 in left, "чужая запись остаётся в общем файле"
