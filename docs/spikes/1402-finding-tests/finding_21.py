"""Повторный redeem обнуляет живой токен стюарда (#1194)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from hub.actionable_errors import chat_pair_invalid_detail
from hub.services import steward_shadow as sh

_STUB_CURL = """#!/bin/bash
for ((i = 1; i <= $#; i++)); do
  arg="${!i}"
  if [[ "$arg" == "-H" ]]; then
    next=$((i + 1))
    header="${!next}"
    if [[ "$header" == Authorization:* ]]; then
      printf '%s\\n' "$header" >> "$SEEN_AUTH"
    fi
  fi
done
for arg in "$@"; do
  if [[ "$arg" == *chat-pair/redeem* ]]; then
    cat "$SPENT_BODY"
    exit 0
  fi
done
printf '%s' '{"ok":true}'
"""


def test_rerunning_redeem_must_not_wipe_a_live_token(tmp_path: Path):
    spent_body = json.dumps({"detail": chat_pair_invalid_detail()})
    assert not re.search(r'"token"\\s*:', spent_body), (
        "предусловие: spent-code 401 не содержит поля token"
    )
    (tmp_path / "spent-401.json").write_text(spent_body)

    token_file = tmp_path / "steward.credential"
    token_file.write_text("live-token-KEEP")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_STUB_CURL)
    curl.chmod(0o755)
    seen = tmp_path / "seen-auth"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SEEN_AUTH"] = str(seen)
    env["SPENT_BODY"] = str(tmp_path / "spent-401.json")

    with patch.object(sh, "CREDENTIAL_PATH", str(token_file)):
        redeem, _check, evidence, _judgement = sh.delivery_commands(
            1194, "AH-SPENTCODE", "https://hub.example"
        )

    subprocess.run(
        ["bash", "-c", redeem],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert token_file.read_text().strip() == "live-token-KEEP", (
        "повторный redeem по 401 spent-code обнулил живой токен"
    )

    subprocess.run(
        ["bash", "-c", evidence],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    headers = seen.read_text().splitlines() if seen.exists() else []
    assert headers == ["Authorization: Bearer live-token-KEEP"], (
        f"следующий curl ушёл с пустым Bearer после обнуления файла: {headers}"
    )
