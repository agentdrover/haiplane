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

import ast
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


# Соединение на запрос (#1065): обработчик обязан брать базу через _db(request),
# а не лезть в app.state.db. Общее соединение на процесс — это ровно та схема,
# при которой commit одной корутины фиксировал незакоммиченное другой, и хаб
# описал эту дыру сам в docstring get_write_lock, отложив её как hardening work.
#
# Разбор идёт токенами, а не подстрокой — по той же причине, что и выше: иначе
# страж ловит собственное описание и объяснения в коде и оказывается удалён
# первым же, кто на него наступит без вины.


def _code_lines_with(path: Path, name: str) -> list[str]:
    """Строки КОДА, где встречается имя ``name``: комментарии и строки не в счёт."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    hits: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string == name:
            hits.append(token.start[0])
    return [
        f"{path.relative_to(REPO_ROOT)}:{n}: {lines[n - 1].strip()}"
        for n in sorted(set(hits))
    ]


def _function_spans(path: Path, names: dict[str, str]) -> list[tuple[int, int]]:
    """Строчные границы функций, которым общее соединение положено по делу."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def test_handlers_do_not_reach_for_the_shared_connection():
    """Роут, взявший соединение приложения, работает не на своём.

    Разрешённые случаи перечислены поимённо и объяснены: подъём (lifespan
    открывает и закрывает соединение приложения), откат в ``_db()`` для прямых
    вызовов из web.py в app.py, телеметрия и фоновые шаги — у них запроса нет
    вовсе. Всё остальное — возврат к общей схеме.
    """
    # Границы берутся у ast, а не списком строк: список маркеров пришлось бы
    # дописывать на каждую правку подъёма, и первый же дописавший превратил бы
    # его в место, куда добавляют, чтобы страж замолчал.
    exempt = {
        "lifespan": "подъём и остановка приложения: соединение здесь и рождается",
        "_db": "объявленный откат для прямых вызовов web.py → app.py",
        "_provision_project_detached": "фон переживает запрос и открывает своё",
    }
    offenders: list[str] = []
    for path in (HUB / "app.py", HUB / "web.py"):
        spans = _function_spans(path, exempt)
        for line in _code_lines_with(path, "state"):
            if "app.state.db" not in line:
                continue
            number = int(line.split(":")[1])
            if any(lo <= number <= hi for lo, hi in spans):
                continue
            offenders.append(line)

    assert not offenders, (
        "обработчик берёт общее соединение приложения вместо соединения "
        "запроса — это возврат к схеме, при которой чужой commit фиксирует "
        "вашу незавершённую работу. Берите _db(request):\n" + "\n".join(offenders)
    )


def test_the_manual_write_lock_is_gone():
    """get_write_lock снят вместе с общим соединением, а не оставлен рядом.

    Лок лежал НА соединении. С соединением на запрос у каждого запроса своё,
    и сериализовать этому локу нечего: он молча перестал работать, продолжая
    выглядеть защитой. Оставить его означало бы держать в коде правило,
    которое ничего не делает, — худший вид гарантии.
    """
    survivors: list[str] = []
    for path in HUB.rglob("*.py"):
        survivors += _code_lines_with(path, "get_write_lock")
    assert not survivors, (
        "get_write_lock вернулся; на соединении запроса он не сериализует "
        "ничего — нужна write_transaction() из hub/db.py:\n" + "\n".join(survivors)
    )
