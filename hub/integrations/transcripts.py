"""Agent transcript monitoring — read latest transcripts for running agents."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hub.config import TRANSCRIPTS_DIR

log = logging.getLogger(__name__)


def _parse_jsonl_tail(path: Path, max_events: int = 20) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(events) >= max_events:
            break
    events.reverse()
    return events


class TranscriptsIntegration:
    """Concrete transcripts plugin backed by local JSONL files."""

    def list_recent_transcripts(self, limit: int = 10) -> list[dict[str, Any]]:
        if not TRANSCRIPTS_DIR.is_dir():
            return []
        files = sorted(
            TRANSCRIPTS_DIR.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        results: list[dict[str, Any]] = []
        for f in files[:limit]:
            events = _parse_jsonl_tail(f, max_events=3)
            last_role = ""
            last_text = ""
            for ev in reversed(events):
                role = ev.get("role", "")
                if role in ("assistant", "user"):
                    last_role = role
                    content = ev.get("content", "")
                    if isinstance(content, str):
                        last_text = content[:200]
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                last_text = str(block.get("text", ""))[:200]
                                break
                    break
            results.append(
                {
                    "path": str(f),
                    "name": f.stem,
                    "modified": f.stat().st_mtime,
                    "last_role": last_role,
                    "last_text": last_text,
                }
            )
        return results

    def transcript_detail(
        self, transcript_path: str, tail_events: int = 30
    ) -> list[dict[str, Any]]:
        p = Path(transcript_path)
        if not p.exists():
            return []
        return _parse_jsonl_tail(p, max_events=tail_events)
