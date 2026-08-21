"""Как код хаба читает строки из базы (#892).

aiosqlite объявляет ``execute_fetchall`` как ``Iterable[Row]``, хотя отдаёт
список. #847 ввёл в ``hub/db.py`` обёртку ``fetchall()`` и перевёл на неё все
вызовы — но ветка, отпочкованная до этого перевода, снова напишет привычный
``execute_fetchall``, локально будет зелена, а develop покраснеет после мержа
(именно так и случилось: два вызова из #877 встретились с базой из #847).

Поэтому правило держится проверкой, а не памятью исполнителя: прямой
``execute_fetchall`` допустим только внутри ``hub/db.py``, где обёртка и
живёт. Тесты хаба этим правилом не связаны — в них результат читают как
попало и mypy их не проверяет; страж стоит на коде, который собирается CI.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB = REPO_ROOT / "hub"

# Единственный модуль, где прямой вызов законен: сама обёртка.
WRAPPER = HUB / "db.py"

FORBIDDEN = "execute_fetchall"


def _direct_calls(path: Path) -> list[str]:
    """Строки файла с прямым вызовом ``execute_fetchall`` — только код.

    Разбор идёт токенами, а не подстрокой: комментарий и докстринг приезжают
    как COMMENT и STRING и до сравнения не доходят. Иначе страж поймал бы
    собственное описание и объяснение в ``hub/db.py`` — и был бы удалён
    первым же, кто на него наступит без вины.
    """
    source = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string == FORBIDDEN:
            number = token.start[0]
            text = lines[number - 1].strip() if number <= len(lines) else ""
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {text}")
    return offenders


def _hub_modules() -> list[Path]:
    return sorted(p for p in HUB.rglob("*.py") if p != WRAPPER)


def test_no_direct_execute_fetchall_outside_db_module():
    """Прямой вызов вне обёртки — это ошибка типов, которая ждёт мержа."""
    offenders: list[str] = []
    for path in _hub_modules():
        offenders += _direct_calls(path)

    assert not offenders, (
        "прямой execute_fetchall возвращает Iterable[Row] и не индексируется "
        '(mypy: Value of type "Iterable[Row]" is not indexable) — берите '
        "fetchall(db, sql, params) из hub/db.py:\n" + "\n".join(offenders)
    )


def test_the_wrapper_module_itself_is_exempt():
    """hub/db.py вызывает execute_fetchall по делу — и не попадает в обход.

    Проверяется и то, что вызов там действительно есть: страж, у которого
    исключение перестало что-либо исключать, молчит одинаково с обеих сторон.
    """
    assert _direct_calls(WRAPPER), (
        "hub/db.py перестал вызывать execute_fetchall — обёртки больше нет "
        "или она переехала, и исключение стража надо пересмотреть вместе с ней"
    )
    assert WRAPPER not in _hub_modules()
