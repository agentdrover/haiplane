"""The Stop-hook's wait file is shared by every session in the directory (#772).

Three sessions worked in this checkout on 2026-08-20 and woke each other four
times, because the hook read every wait in the file regardless of who was
waiting for it — and a session rewriting the file dropped the others' waits,
which is how one task ended up delivered by two agents at once.

The fix is one optional field. These tests pin the part that is easy to get
wrong: a wait WITHOUT an owner must keep behaving exactly as before, or the
change would only work once every session had already adopted it.
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


def _load(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("hub_wait_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_FILE = tmp_path / "hub-wait.json"
    module.LOCK_FILE = tmp_path / "hub-wait.lock"
    module.MAX_WAIT_SEC = 5
    return module


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
    module = _load(tmp_path)
    module.STATE_FILE.write_text(
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

    left = json.loads(module.STATE_FILE.read_text())["waits"]
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
    module = _load(tmp_path)
    module.STATE_FILE.write_text(
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
    assert module.STATE_FILE.exists(), "and it must still be there for its owner"


@pytest.mark.parametrize("payload", [{}, {"session_id": ""}])
def test_without_a_session_id_every_wait_is_ours(
    tmp_path, monkeypatch, capsys, payload
):
    """AC-3 again, from the hook's side: today's behaviour, unchanged."""
    module = _load(tmp_path)
    module.STATE_FILE.write_text(
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
