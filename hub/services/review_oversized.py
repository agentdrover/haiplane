"""Тесты AC в файле с диффом выше потолка: где они лежат и что ревьюер не прочёл (#1933).

Файл выше ``REVIEW_FILE_LINE_CAP`` строк не помещается в один проход, и раньше
ревьюеру говорили «прочитай сколько успеешь». Непрочитанным оказывался хвост
файла, а в хвосте — именно тесты, доказывающие AC (#1874, #1862). Здесь две
чистые вещи, без БД и сети:

* :func:`ac_test_spans` — по диффу находит функции ``test_ref`` задачи и
  отдаёт диапазоны строк ФАЙЛА на коммите сдачи (новая сторона диффа);
* :func:`unread_ac_tests` — из ``lost_dimensions`` отчёта берёт тесты AC,
  которые ревьюер назвал непрочитанными, точным совпадением со списком.

Второй — не разбор прозы (#1238): строка сверяется с ЗАКРЫТЫМ списком из
карточки задачи, после фиксированного префикса, по равенству. Подстрока,
другой регистр префикса и чужое имя не засчитываются.

Границы функций берутся НЕ из текста ханков (это чёрный список случаев:
литералы, вложенность, переносы между ханками), а из ``ast`` файла теста на
ЗАКРЕПЛЁННОМ sha сдачи: ``lineno``, ``end_lineno``, ``decorator_list``, класс —
по дереву. Файл не прочитан или не разобран — «не найден», без отката на ветку.

Локатор СИНТАКСИЧЕСКИЙ: диапазон — по тексту файла, не по исполнению; global в
теле класса, присваивание атрибуту класса, exec/globals() не учитываются
(граница анализа, решение владельца по #1933: перечислением способов связывания
Python она не закрывается).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hub.services.test_locator import PYTEST, runner_of

#: Префикс строки ``lost_dimensions``, которой ревьюер называет непрочитанный тест AC.
UNREAD_PREFIX = "ac_test_unread: "

_PARAM = re.compile(r"\[.*\]$")

NOT_FOUND = "не найден в диффе"
UNREADABLE = "не найден: файл на закреплённом коммите не прочитан или имя определено неоднозначно"
READ_LIMIT = 2_000_000


@dataclass(frozen=True)
class AcTestSpan:
    """Где лежит тест AC: ``start``/``end`` — строки файла на sha сдачи, или ``None``."""

    ref: str
    path: str
    start: int | None = None
    end: int | None = None
    unreadable: bool = False
    lines: tuple[tuple[int, str], ...] = ()

    @property
    def found(self) -> bool:
        return self.start is not None

    def label(self) -> str:
        """Строка блока «ЧИТАТЬ ПЕРВЫМИ» для этого теста."""
        if self.found:
            return f"ЧИТАТЬ ПЕРВЫМИ: {self.path}:{self.start}–{self.end} ({self.ref})"
        why = UNREADABLE if self.unreadable else NOT_FOUND
        return f"ЧИТАТЬ ПЕРВЫМИ: {why} ({self.ref})"


def ac_test_refs(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """``test_ref`` AC с ``verifiable_by=test``, в порядке AC, без повторов."""
    seen: list[str] = []
    for row in rows:
        ref = (row.get("test_ref") or "").strip()
        if str(row.get("verifiable_by") or "") == "test" and ref and ref not in seen:
            seen.append(ref)
    return seen


def _parse_ref(ref: str) -> tuple[str, tuple[str, ...], str] | None:
    """``(файл, цепочка классов, функция)`` pytest-ссылки; параметр ``[id]`` отброшен."""
    if runner_of(ref) != PYTEST:
        return None
    path, _, node = ref.partition("::")
    parts = [part for part in _PARAM.sub("", node).split("::") if part]
    if not parts:
        return None
    return path, tuple(parts[:-1]), parts[-1]


def ac_test_paths(refs: Iterable[str]) -> list[str]:
    """Файлы pytest-ссылок, без повторов, в порядке ссылок."""
    out: list[str] = []
    for ref in refs:
        parsed = _parse_ref(ref)
        if parsed and parsed[0] not in out:
            out.append(parsed[0])
    return out


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (*_DEFS, ast.ClassDef)
_MATCH_NAMES = tuple(
    getattr(ast, n) for n in ("MatchAs", "MatchStar") if hasattr(ast, n)
)
MAX_NODES = 200_000  # предел обхода: больше - «не прочитан», а не долгая работа


class Unknown(Exception):
    """Имя нельзя свести к ровно одному простому ``def``/``class``: не гадаем."""


def _binds(node: ast.AST, name: str) -> bool:
    """Связывает ли узел ``name`` в своей области - ЛЮБЫМ способом, который знает ``ast``."""
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, _SCOPES):
        return node.name == name
    if isinstance(node, ast.alias):
        return node.name == "*" or (node.asname or node.name).split(".")[0] == name
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return name in node.names
    if _MATCH_NAMES and isinstance(node, _MATCH_NAMES):
        return getattr(node, "name", None) == name
    return isinstance(node, getattr(ast, "MatchMapping", ())) and node.rest == name  # type: ignore[attr-defined]


def _header(node: ast.AST) -> list[ast.AST]:
    """Выражения def/class/lambda, исполняемые во ВНЕШней области (декораторы, значения
    по умолчанию, аннотации, основания класса): walrus в них связывает имя снаружи."""
    if isinstance(node, _DEFS):
        return [
            *node.decorator_list,
            node.args,
            *([node.returns] if node.returns else []),
        ]
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    if isinstance(node, ast.Lambda):
        return [node.args]
    return []


_COMP = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scan_scope(body: Sequence[ast.stmt], name: str) -> tuple[list[ast.stmt], int]:
    """``(прямые def/class с этим именем, число прочих связываний)`` в теле области.

    Обход итеративный (стек, не рекурсия: глубокая цепочка ``elif`` ломала
    рекурсивный) и с пределом узлов. Тела вложенных функций, классов и лямбд не
    открываются (другая область), а их заголовки - открываются; ``def``/``class``
    внутри ``if``/``match``/``try`` этой области - связывание этой области. Цель
    comprehension локальна для него, а walrus в нём связывает имя снаружи.
    """
    direct: list[ast.stmt] = []
    others, seen = 0, 0
    stack: list[tuple[ast.AST, bool]] = []
    for stmt in body:
        if isinstance(stmt, _SCOPES):
            direct += [stmt] if stmt.name == name else []
            stack.extend((h, False) for h in _header(stmt))
        else:
            stack.append((stmt, False))
    while stack:
        node, comp = stack.pop()
        seen += 1
        if seen > MAX_NODES:
            raise Unknown("файл слишком велик для разбора")
        if isinstance(node, (*_SCOPES, ast.Lambda)):
            others += isinstance(node, _SCOPES) and node.name == name
            stack.extend((h, comp) for h in _header(node))
            continue
        if comp:
            others += (
                isinstance(node, ast.NamedExpr)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            )
        else:
            others += _binds(node, name)
        inner = comp or isinstance(node, _COMP)
        stack.extend((c, inner) for c in ast.iter_child_nodes(node))
    return direct, others


def _resolve(
    tree: ast.Module, owners: Sequence[str], name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Функция теста по БЕЛОМУ списку (#1933): имя связано ровно одним простым def.

    Метод - ровно одним ``def`` в теле ровно одного ``class`` (цепочка владельцев
    от верха модуля). Любое другое связывание имени в той же области - ``Unknown``;
    ``None`` - имени в области нет вовсе.
    """
    scope: Sequence[ast.stmt] = tree.body
    for owner in owners:
        direct, others = _scan_scope(scope, owner)
        if not direct and not others:
            return None
        if len(direct) != 1 or others or not isinstance(direct[0], ast.ClassDef):
            raise Unknown(owner)
        scope = direct[0].body
    direct, others = _scan_scope(scope, name)
    if not direct and not others:
        return None
    if len(direct) != 1 or others or not isinstance(direct[0], _DEFS):
        raise Unknown(name)
    return direct[0]


def _tree_of(text: str | None) -> tuple[ast.Module, list[str]] | None:
    if text is None:
        return None
    try:
        # Нумерация строк ast — по \n, \r\n, \r; splitlines() режет ещё и по \x0c.
        return ast.parse(text), re.split(r"\r\n|\r|\n", text)
    except Exception:  # noqa: BLE001 - любой сбой разбора = «не прочитан»
        return None


def ac_test_spans(
    sources: Mapping[str, str | None] | None, refs: Sequence[str]
) -> list[AcTestSpan]:
    """Диапазоны тестов AC по файлам на закреплённом sha, в порядке ``refs``.

    ``sources`` — ``{путь: текст файла на sha}``; ``None`` вместо текста (или
    ``sources=None``) — файл не прочитан, это «неизвестно», а не «теста нет».
    Ссылка не pytest (vitest) и функция, которой в файле нет, — без диапазона:
    молчание выглядело бы как «ревьюеру читать нечего».
    """
    trees: dict[str, tuple[ast.Module, list[str]] | None] = {}
    out: list[AcTestSpan] = []
    for ref in refs:
        parsed = _parse_ref(ref)
        if parsed is None:
            out.append(AcTestSpan(ref, ""))
            continue
        path, owners, name = parsed
        if path not in trees:
            trees[path] = _tree_of((sources or {}).get(path))
        got = trees[path]
        if got is None:
            out.append(AcTestSpan(ref, path, unreadable=True))
            continue
        try:
            node = _resolve(got[0], owners, name)
            if node is None:
                out.append(AcTestSpan(ref, path))
                continue
            decorated = [d.lineno for d in node.decorator_list]
            start, end = min([node.lineno, *decorated]), node.end_lineno or node.lineno
            body = tuple((n, got[1][n - 1]) for n in range(start, end + 1))
            out.append(AcTestSpan(ref, path, start, end, lines=body))
        except Exception:  # noqa: BLE001 - Unknown, RecursionError, IndexError: «не прочитан»
            out.append(AcTestSpan(ref, path, unreadable=True))
    return out


def covered_lines(section: str) -> list[tuple[int, int]]:
    """Диапазоны строк НОВОЙ стороны, которые текст диффа файла показывает (по ``@@ +c,d``)."""
    out = []
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", section, re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        if count:
            out.append((start, start + count - 1))
    return out


def shown_in(section: str | None, span: AcTestSpan) -> bool:
    """Показан ли тест AC в тексте диффа файла ЦЕЛИКОМ (иначе нужна вырезка)."""
    if section is None or span.start is None or span.end is None:
        return False
    return any(a <= span.start and span.end <= b for a, b in covered_lines(section))


async def read_sources(
    git: Any, repo: str | None, sha: str, refs: Iterable[str]
) -> dict[str, str | None] | None:
    """Тексты файлов тестов AC на закреплённом sha; ``None`` вместо текста — не прочитан.

    Нет sha или клона — ``None`` целиком. Усечённое чтение не разбирается: обрезанный
    файл мог бы дать чужие границы.
    """
    paths = ac_test_paths(refs)
    if not paths:
        return {}
    if not sha or not repo:
        return None
    out: dict[str, str | None] = {}
    for path in paths:
        try:
            got = await git.read_file_at_ref(repo, sha, path, limit_chars=READ_LIMIT)
        except Exception:  # noqa: BLE001 - "could not look" is an answer, not a crash
            got = {}
        ok = got.get("state") == "present" and not got.get("truncated")
        out[path] = got.get("content", "") if ok else None
    return out


def oversized_paragraph(
    oversized: list[tuple[str, int]], has_ac_tests: bool, cap: int
) -> str:
    """Абзац про файлы выше потолка: порядок чтения и что называть непрочитанным (#1933)."""
    named = ", ".join(f"{path} ({count} строк)" for path, count in oversized)
    order = (
        "Порядок: 1) тесты AC, названные выше строками «ЧИТАТЬ ПЕРВЫМИ» "
        "(номера — строки файла на коммите сдачи; «не найден в диффе» — "
        "найди сам); 2) остальные файлы диффа; 3) остаток большого файла. "
        if has_ac_tests
        else "Порядок: 1) остальные файлы диффа; 2) остаток большого файла. "
    )
    unread = (
        f"; непрочитанный тест AC — отдельной строкой «{UNREAD_PREFIX}"
        "<test_ref>» (test_ref ровно как в скобках блока)"
        if has_ac_tests
        else ""
    )
    return (
        f"НЕ ПОМЕСТЯТСЯ В ОДИН ПРОХОД (потолок {cap} "
        f"строк на файл): {named}. Потолок — порядок чтения, а не предел. "
        f"{order}Второй проход не гарантирован (добор лестницы возможен, но на него не рассчитывай). Непрочитанный остаток "
        f"перечисли в lost_dimensions и сдай incomplete=true{unread}. "
        "Один большой файл не имеет права съесть бюджет, которого ждали "
        "остальные."
    )


def first_block(
    spans: Sequence[AcTestSpan], sha: str = "", inline: bool = False
) -> str:
    """Блок «ЧИТАТЬ ПЕРВЫМИ»: шапка с sha сдачи и по строке на тест AC.

    Номера строк - файла на коммите сдачи. Облачный ревьюер читает подвижную
    ветку командой, поэтому шапка требует сверить HEAD с sha и при расхождении
    не верить номерам (читать ``git show <sha>:<путь>`` или назвать тест
    непрочитанным). У локального ревьюера клона нет - сверять нечего.
    """
    if not spans:
        return ""
    syntactic = (
        "ЧИТАТЬ ПЕРВЫМИ — диапазон — по тексту файла, не по исполнению; global в теле "
        "класса, присваивание атрибуту класса, exec/globals() не учитываются.\n"
    )
    short = sha[:12]
    if not short:
        head = ""
    elif inline:
        head = (
            f"ЧИТАТЬ ПЕРВЫМИ — номера строк относятся к файлам на коммите сдачи {short}; "
            "клона у тебя нет, git не используй.\n"
        )
    else:
        head = (
            f"ЧИТАТЬ ПЕРВЫМИ — номера строк относятся к коммиту сдачи {short}. "
            "Перед чтением сверь `git rev-parse HEAD` с ним: при несовпадении номерам "
            f"не верь — читай тест командой `git show {short}:<путь>` или назови его "
            f"в lost_dimensions «{UNREAD_PREFIX}<test_ref>».\n"
        )
    return head + syntactic + "\n".join(span.label() for span in spans)


def cutout(span: AcTestSpan, refs: Sequence[str] = ()) -> str:
    """Функция теста AC вырезкой: строки файла с номерами; ``refs`` - все ссылки диапазона."""
    names = "; ".join(refs) if refs else span.ref
    body = "\n".join(f"{number}: {text}" for number, text in span.lines)
    return (
        f"=== ВЫРЕЗКА ТЕСТА AC {names} ({span.path}:{span.start}–{span.end}) ===\n"
        f"{body}\n=== КОНЕЦ ВЫРЕЗКИ ===\n"
    )


def unread_ac_tests(
    lost_dimensions: Iterable[str], test_refs: Sequence[str]
) -> list[str]:
    """Тесты AC, названные ревьюером непрочитанными, — точным совпадением (#1933).

    Строка ``lost_dimensions`` засчитывается, если она начинается ровно с
    ``UNREAD_PREFIX`` и остаток (без пробелов по краям) равен одному из
    ``test_refs`` задачи. Ответ — в порядке ``test_refs``, без повторов.
    """
    named = {
        line[len(UNREAD_PREFIX) :].strip()
        for line in lost_dimensions
        if isinstance(line, str) and line.startswith(UNREAD_PREFIX)
    }
    return [ref for ref in test_refs if ref in named]


def names_unread_ac_test(lost_dimensions: Iterable[str]) -> bool:
    """Назвал ли ревьюер хоть один непрочитанный тест AC (по префиксу)."""
    return any(
        isinstance(line, str) and line.startswith(UNREAD_PREFIX)
        for line in lost_dimensions
    )


async def unread_for_task(
    db: Any, task_id: int, lost_dimensions: Iterable[str]
) -> list[str]:
    """Тесты AC задачи, названные непрочитанными: точная сверка с её test_ref (#1933).

    Запрос к БД делается только когда в отчёте есть строка с префиксом.
    """
    lost = list(lost_dimensions)
    if not names_unread_ac_test(lost):
        return []
    from hub import repository as repo

    rows = await repo.list_acceptance_criteria(db, task_id)
    return unread_ac_tests(lost, ac_test_refs([dict(r) for r in rows]))


async def attach_ac_tests_unread(db: Any, view: Any) -> None:
    """Заполнить ``view.ac_tests_unread`` из ``lost_dimensions`` и AC задачи."""
    view.ac_tests_unread = await unread_for_task(db, view.task_id, view.lost_dimensions)


RangeKey = tuple[str, int, int]


def _reserve_ac(
    ranges: dict[RangeKey, list[AcTestSpan]],
    cost: dict[RangeKey, int],
    texts: dict[str, str],
    ac_paths: list[str],
    budget: int,
) -> tuple[int, set[RangeKey], set[str], set[RangeKey]]:
    """Шаг A: представление каждого AC-файла - ДЕШЁВОЕ из двух, до файлов без AC.

    Полный дифф файла (плюс непокрытые ханками диапазоны) против НАБОРА его
    вырезок. Полный дифф берётся, когда он не дороже всех вырезок и влезает; иначе
    - помещающееся подмножество вырезок. Так обычный файл не вытесняет более
    дешёвое представление AC и не остаётся без места рядом с ним.
    """
    placed: set[RangeKey] = set()
    full: set[str] = set()
    covered_all: set[RangeKey] = set()
    for path in ac_paths:
        keys = [k for k in ranges if k[0] == path]
        covered = {
            k for k in keys if path in texts and shown_in(texts[path], ranges[k][0])
        }
        cuts_total = sum(cost[k] for k in keys)
        full_cost = len(texts.get(path, "")) + sum(
            cost[k] for k in keys if k not in covered
        )
        if path in texts and full_cost <= cuts_total and full_cost <= budget:
            budget -= full_cost
            full.add(path)
            covered_all |= covered
            placed |= {k for k in keys if k not in covered}
            continue
        for k in keys:
            if cost[k] <= budget:
                placed.add(k)
                budget -= cost[k]
    return budget, placed, full, covered_all


def _upgrade_to_full(
    ranges: dict[RangeKey, list[AcTestSpan]],
    cost: dict[RangeKey, int],
    texts: dict[str, str],
    placed: set[RangeKey],
    budget: int,
    ac_paths: list[str],
    done: set[str],
) -> tuple[int, set[str], set[RangeKey]]:
    """Шаг C: только РАСШИРЕНИЕ с неотрицательной доплатой, из остатка (``placed`` меняется).

    Файлы, чьё представление выбрано на шаге A (``done``), не трогаются.
    """
    upgraded: set[str] = set()
    read_by_diff: set[RangeKey] = set()
    for path in ac_paths:
        if path not in texts or path in done:
            continue
        keys = [k for k in ranges if k[0] == path]
        covered = {k for k in keys if shown_in(texts[path], ranges[k][0])}
        kept = [k for k in keys if k not in covered and k in placed]
        need = len(texts[path]) + sum(cost[k] for k in kept)
        delta = need - sum(cost[k] for k in keys if k in placed)
        if 0 <= delta <= budget:
            budget -= delta
            upgraded.add(path)
            read_by_diff |= covered
            placed -= covered
    return budget, upgraded, read_by_diff


def _assemble(
    units: dict[str, str],
    names: dict[str, str],
    ac_paths: list[str],
    upgraded: set[str],
    shown_plain: set[str],
) -> tuple[list[str], list[str]]:
    """Текст диффа по порядку разделов (файл целиком) и имена не вошедших файлов."""
    text: list[str] = []
    omitted: list[str] = []
    for key, body in units.items():
        if key in ac_paths:
            keep = key in upgraded
        else:
            keep = key in shown_plain
        (text if keep else omitted).append(body if keep else names[key])
    return text, omitted


def fit_with_ac_cutouts(
    sections: list[tuple[str, str]],
    ceiling: int,
    spans: list[AcTestSpan],
) -> tuple[str, list[str], list[str], list[str]]:
    """Файлы под потолок; бюджет тестов AC считается ПО УНИКАЛЬНЫМ ДИАПАЗОНАМ (#1933).

    ``(текст, не вошли, вырезки, непрочитанные тесты AC)``. Единица учёта -
    диапазон ``(путь, начало, конец)``; ссылки ``test_ref`` хранятся при нём
    отдельно (parametrize даёт много ссылок на один диапазон и платится он один
    раз). Приоритеты по шагам, порядок разделов диффа результат не меняет:

    A) представление каждого AC-файла - ДЕШЁВОЕ из двух (полный дифф файла плюс
       непокрытые ханками диапазоны, либо набор его вырезок), выбранное и
       оплаченное ДО шага B; вырезки - помещающееся подмножество (не
       поместившийся диапазон не блокирует остальные: непрочитанными названы
       только его ссылки);
    B) все файлы без тестов AC - от меньшего к большему, затем по пути, из остатка;
    C) последним шагом - только расширение AC-файла (вырезки -> полный дифф) с
       неотрицательной доплатой из остатка; покрытые ханками диапазоны тогда
       прочитаны через дифф.
    """

    def norm(t: str) -> str:
        return t if t.endswith("\n") else t + "\n"

    ranges: dict[RangeKey, list[AcTestSpan]] = {}
    for sp in spans:
        if sp.found and sp.start is not None and sp.end is not None:
            ranges.setdefault((sp.path, sp.start, sp.end), []).append(sp)
    texts: dict[
        str, str
    ] = {}  # раздел = ФАЙЛ целиком: повтор пути (symlink -> файл) склеен
    names: dict[str, str] = {}
    for i, (p, t) in enumerate(sections):
        key = p or f"\0{i}"
        texts[key] = texts.get(key, "") + norm(t)
        names[key] = p or "(часть диффа без имени файла)"

    def rtext(key: tuple[str, int, int]) -> str:
        return cutout(ranges[key][0], [x.ref for x in ranges[key]])

    cost = {k: len(rtext(k)) for k in ranges}
    ac_paths = list(dict.fromkeys(k[0] for k in ranges))
    budget, placed, upgraded, read_by_diff = _reserve_ac(
        ranges, cost, texts, ac_paths, ceiling
    )
    shown_plain: set[str] = set()
    # B: файлы без AC - от меньшего к большему (порядок разделов состав не меняет)
    for key in sorted(
        (k for k in texts if k not in ac_paths), key=lambda k: (len(texts[k]), k)
    ):
        if len(texts[key]) <= budget:
            shown_plain.add(key)
            budget -= len(texts[key])
    budget, more, more_read = _upgrade_to_full(
        ranges, cost, texts, placed, budget, ac_paths, set(upgraded)
    )
    upgraded |= more
    read_by_diff |= more_read
    text, omitted = _assemble(texts, names, ac_paths, upgraded, shown_plain)
    cuts = [rtext(k) for k in ranges if k in placed]
    unread = [
        x.ref
        for k in ranges
        if k not in placed and k not in read_by_diff
        for x in ranges[k]
    ]
    return "".join(text), omitted, cuts, unread
