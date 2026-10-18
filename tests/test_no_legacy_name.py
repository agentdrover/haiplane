"""Страж Волны 5 ребрендинга: в HEAD нет ни одного вхождения старого имени.

Проверяются и имена файлов, и содержимое всех файлов под git ls-files
(регистронезависимо). Старое имя собрано конкатенацией, чтобы тест
не ловил сам себя.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Снимок служб прода (#1849) фиксирует настоящие имена прода (имя юнита,
# каталог в /opt): без них он перестал бы быть снимком. Исключение - ровно один
# префикс каталога; test_the_legacy_name_exemption_is_one_prefix держит его узким.
LEGACY_NAME_EXEMPT_PREFIXES = ("deploy/systemd/prod/",)


def is_exempt(path: str) -> bool:
    return path.startswith(LEGACY_NAME_EXEMPT_PREFIXES)


def test_head_contains_no_legacy_name() -> None:
    legacy = "open" + "claw"  # собрано, чтобы тест не ловил сам себя
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    files = [entry for entry in result.stdout.split("\0") if entry]
    offenders: list[str] = []
    for path in files:
        if is_exempt(path):
            continue
        if legacy in path.lower():
            offenders.append(f"{path} (имя файла)")
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        text = full.read_bytes().decode("utf-8", errors="ignore").lower()
        count = text.count(legacy)
        if count:
            offenders.append(f"{path}: {count} вхожд.")
    assert not offenders, "\n".join(offenders)


def test_the_legacy_name_exemption_is_one_prefix() -> None:
    assert LEGACY_NAME_EXEMPT_PREFIXES == ("deploy/systemd/prod/",)
    assert is_exempt("deploy/systemd/prod/etc/systemd/system/x.service")
    for outside in (
        "deploy/systemd/prod",
        "deploy/systemd/prodx/a",
        "deploy/systemd/a",
        "deploy/systemd/prod-old/a",
        "deploy/a",
        "docs/deploy/systemd/prod/a",
        "tests/test_no_legacy_name.py",
    ):
        assert not is_exempt(outside), outside
