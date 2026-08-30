#!/usr/bin/env python3
"""CI check: no secret lands in the tree unnoticed (#1080).

Одно определение проверки для `make security` и шага CI «Secret scan» — два
места, зовущие один скрипт, не могут разъехаться. Раньше тело жило прямо в
ci.yml, и локальный `make lint`/`make check` не видел его вовсе: зелёная
локальная проверка ничего не говорила про этот шаг, и CI мог покраснеть на
находке, которую никто не гонял до сдачи.

    uv run python scripts/secret_scan.py

Exit 0 без секретов, exit 1 и печать находок иначе.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    proc = subprocess.run(
        [
            "detect-secrets",
            "scan",
            "--exclude-files",
            r"(^uv\.lock$|^\.pytest_cache/|^\.ruff_cache/)",
        ],
        capture_output=True,
        text=True,
    )
    report = json.loads(proc.stdout)
    results = report.get("results", {})
    if results:
        print(json.dumps(results, indent=2))
        return 1
    print("No secrets detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
