"""Что именно опубликовано в активной версии скилла (#1169).

Текст скилла — инструкция, которую хаб раздаёт агентам через
``hub_get_skill``. Активной, то есть раздаваемой, версия становится тремя
путями (создание человеком, активация чужого драфта, сид), и ни на одном из
них не было видно, ЧТО изменилось относительно прежней активной версии.
Модуль даёт двум разным по роли вещам одно место:

* **Диф** — основное. Он показывает изменение и ни от каких эвристик не
  зависит: сводка числа добавленных и удалённых строк плюс unified diff.
* **Скан содержимого** — вспомогательное. Он НЕ выносит вердикт
  «безопасно», а наводит взгляд на место внутри дифа, потому что диф на
  100k символов человеком не читается. Пустой перечень сработавших правил
  означает «ни один регэксп не совпал», а не «проверено» — поэтому у отчёта
  есть поле ``note``, говорящее это словами там, где отчёт читают.

Модуль намеренно чистый: никаких обращений к базе и никаких импортов из
``hub`` — его зовут и ``hub/app.py`` (пути 1 и 2), и ``hub/db.py`` (путь 3,
сид), а ``hub.db`` не может импортировать ``hub.repository``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Состояние «сравнивать не с чем» — своё, а не пустой диф и не диф, в котором
# добавлено всё. Первый скилл в реестре и сид, ставящий версию 1, попадают
# именно сюда, и человек должен видеть разницу между «ничего не изменилось» и
# «прежней активной версии не было».
BASELINE_ABSENT = "absent"
BASELINE_VERSION = "version"
# Третье состояние — и оно не про расчёт, а про ЧТЕНИЕ записи: событие
# ``skill_activated``, написанное до #1169, несёт только имя и версию. «Дифа в
# записи нет» и «диф, в котором ничего не изменилось» — разные ответы, и
# показывать второй вместо первого значит утверждать факт, которого нет.
BASELINE_UNRECORDED = "unrecorded"
# Четвёртое — и самое частое в день выката. Запись о публикации может
# отсутствовать ЦЕЛИКОМ, а не только не нести дифа: сид до #1169 не писал
# ``skill_activated`` вовсе, и обе версии реестра хаба активны без единого
# события. Разница с ``BASELINE_UNRECORDED`` не косметическая: там запись есть
# и в ней не записан диф, здесь записи нет вообще. Показывать это состояние
# пустым местом значит оставлять человека выбирать между «ничего не менялось»
# и «блок не построился» — а молчание не отличимо ни от того, ни от другого
# (#1169, находка ревью #327).
BASELINE_NO_RECORD = "no_record"
# Пятое — и единственное, где хаб отказывается считать. ``SequenceMatcher``
# квадратичен, и ускорить его нельзя: такова его природа. Замер на входе
# «строки переставлены через одну» (a = range(n), b = a[::2] + a[1::2]),
# python 3.14, этот же ноутбук:
#
#     строк  1000,   4 КБ →  0.04 с
#     строк  6000,  28 КБ →  1.26 с
#     строк 16000,  83 КБ →  9.39 с
#
# Вызов синхронный и стоит внутри асинхронного обработчика, так что открытие
# страницы скилла с таким текстом занимает рабочий процесс на десяток секунд,
# а ``_skill_publish_views`` делает ту же работу второй раз в ``unified_diff``.
# Отказ считать — СВОЁ состояние, а не «+0/−0»: соврать числами здесь значит
# воспроизвести ровно тот дефект, ради которого заведена задача (#1169).
BASELINE_TOO_LARGE = "too_large"

BASELINE_ABSENT_NOTE = "прежней активной версии нет — сравнивать не с чем"
BASELINE_NO_RECORD_NOTE = (
    "эта версия стала активной до того, как хаб начал записывать публикации: "
    "ни дифа, ни вердикта по ней не записано — не потому, что менять было "
    "нечего, а потому, что запись тогда не велась"
)
BASELINE_TOO_LARGE_NOTE = (
    "версии слишком велики и различаются слишком сильно, чтобы посчитать диф "
    "здесь: это НЕ «изменений нет» — что именно изменилось, не посчитано; обе "
    "версии лежат в реестре целиком и сравнимы вручную"
)
BASELINE_UNRECORDED_NOTE = (
    "запись о публикации сделана до того, как хаб начал записывать диф: что "
    "именно изменилось тогда, не записано"
)
SCAN_UNRECORDED_NOTE = (
    "вердикта в этой записи нет: она сделана до того, как хаб начал его "
    "записывать — это не «правил не сработало»"
)
SCAN_NOTE = (
    "перечень сработавших правил, а не оценка безопасности: пустой список "
    "означает «ни одно правило не совпало», а не «проверено»"
)

# Сколько символов текста вокруг совпадения показывать как фрагмент.
_FRAGMENT_PAD = 40

# Потолок работы, за которым диф не считается вовсе. Единица — «клетка»
# матрицы сравнения: произведение длин ОСТАТКОВ после отсечения общего начала
# и общего хвоста. Число выбрано замером, а не на глаз (см. BASELINE_TOO_LARGE):
#
#   вход                                клеток   время
#   перестановка через одну, 1000 строк    1.0M   0.04 с
#   перестановка через одну, 2000 строк    4.0M   0.14 с   ← потолок здесь
#   перестановка через одну, 6000 строк   36.0M   1.23 с
#   перестановка через одну, 16000 строк 255.9M   8.90 с
#
# То есть потолок держит худший случай в пределах ~0.15 с вместо десятка
# секунд. Оценка НАМЕРЕННО грубая и в безопасную сторону: те же 4.0M клеток на
# полностью переписанном файле в 2000 строк и на файле из коротких
# повторяющихся строк считались за 0.00 с (там срабатывает autojunk difflib), и
# такие пары отказ задевает зря. Цена ошибки несимметрична: лишний отказ
# человек читает словами, а десятисекундная пауза съедает рабочий процесс.
_DIFF_BUDGET_CELLS = 4_000_000


@dataclass(frozen=True)
class Rule:
    """Одно ИМЕНОВАННОЕ правило скана — по духу как scripts/secret_scan.py.

    Общего балла нет намеренно: балл нечем показать внутри дифа, а имя
    правила с фрагментом и смещением — можно.
    """

    name: str
    title: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ScanHit:
    """Срабатывание правила: чем сработало, что совпало и где именно."""

    rule: str
    title: str
    fragment: str
    offset: int
    line: int

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "title": self.title,
            "fragment": self.fragment,
            "offset": self.offset,
            "line": self.line,
        }


@dataclass(frozen=True)
class DiffSummary:
    """Сводка изменения к прежней активной версии.

    ``baseline`` — дискриминатор состояния, а не украшение: при
    ``BASELINE_ABSENT`` и ``BASELINE_TOO_LARGE`` счётчики равны ``None``,
    потому что «добавлено 0» и «добавлено всё» — оба неверные ответы на
    вопрос, на который ответа нет: в первом случае его не было, во втором его
    не посчитали.
    """

    baseline: str
    baseline_version: int | None
    added_lines: int | None
    removed_lines: int | None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "baseline": self.baseline,
            "baseline_version": self.baseline_version,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
        }
        if self.baseline == BASELINE_ABSENT:
            payload["note"] = BASELINE_ABSENT_NOTE
        if self.baseline == BASELINE_TOO_LARGE:
            payload["note"] = BASELINE_TOO_LARGE_NOTE
        return payload


def _rule(name: str, title: str, pattern: str) -> Rule:
    return Rule(name, title, re.compile(pattern, re.IGNORECASE | re.MULTILINE))


# Стартовый набор. Правила ловят ФОРМУЛИРОВКУ, а не намерение, и потому
# намеренно узкие: каждое требует глагола рядом с объектом, иначе любой текст
# про рабочий процесс (а реестр хаба состоит ровно из таких) срабатывал бы на
# упоминании слова «токены». Качество конкретных регэкспов уточняется по
# реальным находкам — набор переписывается по пропущенному случаю, а не
# расширяется впрок.
RULES: tuple[Rule, ...] = (
    _rule(
        "external_fetch",
        "обращение к внешней сети",
        r"(?:\b(?:curl|wget|скачай|скачать|загрузи|выкачай|fetch|download|"
        r"открой|перейди)\b[^\n]{0,80}https?://)"
        r"|(?:\brequests\.(?:get|post|put)\s*\()"
        r"|(?:\burllib\.request\b)"
        r"|(?:\bfetch\s*\(\s*[\"']https?://)",
    ),
    _rule(
        "secret_exfiltration",
        "чтение и отправка ключей, токенов или переменных окружения",
        r"(?:\bos\.environ\b)|(?:\bgetenv\s*\()|(?:\bprocess\.env\b)"
        r"|(?:\bprintenv\b)"
        r"|(?:\b(?:отправь|перешли|вышли|опубликуй|send|post|upload|exfiltrate)"
        r"\b[^\n]{0,60}\b(?:токен|ключ|секрет|пароль|token|secret|api[_ -]?key|"
        r"password|credential)\w*)"
        r"|(?:~?/?\.ssh/|\bid_rsa\b|\.aws/credentials|\.netrc\b)"
        r"|(?:\b[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)\b)",
    ),
    _rule(
        "task_status_bypass",
        "смена статуса задачи в обход hub_report_done",
        r"(?:\bhub_force_complete_task\b)|(?:\bhub_decide_task\b)"
        r"|(?:\bUPDATE\s+tasks\s+SET\b[^\n]{0,60}\bstatus\b)"
        r"|(?:\b(?:в\s+обход|минуя|не\s+вызывая)\b[^\n]{0,40}"
        r"\bhub_report_done\b)"
        r"|(?:\b(?:постав|смени|измени|обнови|проставь)\w*\b[^\n]{0,40}"
        r"\bстатус\w*\b[^\n]{0,40}\b(?:completed|завершён|завершен|готово)\b)",
    ),
    _rule(
        "self_approval",
        "самоодобрение ревью",
        # Местоимение обязано вести К ОБЪЕКТУ — «одобри свою РАБОТУ», — а не
        # просто стоять рядом с глаголом. Разница измерена, а не предположена:
        # прежняя редакция ловила «автор сдаёт работу, ревьюер — другой агент;
        # approve не свой», то есть текст, говорящий ровно обратное. Реестр
        # хаба состоит из инструкций про ревью, и правило, срабатывающее на
        # словаре предметной области, приучает человека пролистывать вердикт —
        # чем обесценивает и те случаи, ради которых он заведён.
        r"(?:\bhub_approve_task\b)"
        r"|(?:\b(?:одобри|подтверди|approve)\w*\b[^\n]{0,30}"
        r"\b(?:сво[йюяее]\w*|собственн\w+|own|self)\s+\w)"
        r"|(?:\bсам\w*\s+себ[яе]\b)"
        r"|(?:\bself[-\s]?approv\w*)"
        r"|(?:\b(?:одобри|подтверди)\w*\s+(?:сам|само)\b)",
    ),
)


def _line_of(text: str, offset: int) -> int:
    """Номер строки (с единицы), в которой начинается совпадение."""
    return text.count("\n", 0, offset) + 1


def scan_content(content: str) -> list[ScanHit]:
    """Прогнать стартовый набор правил по тексту скилла.

    Каждое правило даёт не больше одного срабатывания: цель — указать вход в
    диф, а не перечислить все вхождения; двадцать одинаковых указателей вход
    не облегчают.
    """
    hits: list[ScanHit] = []
    for rule in RULES:
        match = rule.pattern.search(content)
        if match is None:
            continue
        start = match.start()
        left = max(0, start - _FRAGMENT_PAD)
        right = min(len(content), match.end() + _FRAGMENT_PAD)
        hits.append(
            ScanHit(
                rule=rule.name,
                title=rule.title,
                fragment=content[left:right].replace("\n", " ").strip(),
                offset=start,
                line=_line_of(content, start),
            )
        )
    return hits


def scan_report(content: str) -> dict[str, object]:
    """Вердикт скана в том виде, в каком он ложится в событие и в UI."""
    return {
        "rules_triggered": [hit.as_dict() for hit in scan_content(content)],
        "note": SCAN_NOTE,
    }


def split_lines(text: str) -> list[str]:
    """Разбить текст на строки БЕЗ ПОТЕРИ — то есть обратимо.

    Это не косметика, а условие правильности сводки. ``str.splitlines()``
    лоссовый: ``"instruction"`` и ``"instruction\n"`` дают один и тот же
    список, и правка, состоящая ровно в переводе строки, показывалась как
    «+0/−0» при пустом unified diff — то есть как «ничего не изменилось» о
    настоящем изменении (#1169, находка ревью, сдача 5). Замер до правки:

        summarize_change(previous_content="instruction", content="instruction\n")
        → {'baseline': 'version', 'added_lines': 0, 'removed_lines': 0}
        unified_diff(...) → ''

    ``str.split("\n")`` обратим: ``"\n".join(split_lines(t)) == t`` для любого
    ``t``. Значит «списки строк совпали» равносильно «тексты совпали», и
    сводка не может сказать «изменений нет» о тексте, который изменился.
    Разница проявляется хвостовым пустым элементом: у ``"instruction\n"`` он
    есть, у ``"instruction"`` — нет.

    Побочно обратимость делает видимой и смену CRLF на LF: ``splitlines()``
    съедает ``\r``, ``split("\n")`` оставляет его внутри строки.
    """
    return text.split("\n")


def _trimmed(a: list[str], b: list[str]) -> tuple[int, int]:
    """Длины остатков после отсечения общего начала и общего хвоста.

    Общие края никакого вклада в диф не дают, а стоят O(n) вместо O(n·m) —
    поэтому обычный случай (правка нескольких строк внутри большого скилла)
    после отсечения превращается в почти пустую задачу. Замер: одна вставка в
    файл на 20000 строк даёт 0 клеток и 0.01 с.
    """
    head = 0
    shortest = min(len(a), len(b))
    while head < shortest and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < shortest - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    return len(a) - head - tail, len(b) - head - tail


def diff_is_affordable(previous_content: str, content: str) -> bool:
    """Стоит ли эта пара версий того, чтобы считать по ней диф.

    Оценка стоит O(n+m) и потому не воспроизводит ту беду, от которой
    защищает. Ответ ОДИН на сводку и на unified diff — иначе страница
    отказалась бы считать сводку и тут же потратила бы те же секунды на диф
    под ней (``_skill_publish_views`` зовёт обе функции подряд).
    """
    rest_a, rest_b = _trimmed(split_lines(previous_content), split_lines(content))
    return rest_a * rest_b <= _DIFF_BUDGET_CELLS


def summarize_change(
    *,
    previous_content: str | None,
    previous_version: int | None,
    content: str,
) -> DiffSummary:
    """Сводка «сколько добавлено и удалено» — или явное «сравнивать не с чем».

    Сводка стоит впереди дифа осознанно: на переписанной целиком версии диф
    равен всему содержимому и не добавляет ничего, а два числа отличают
    правку от переписывания сразу.
    """
    if previous_content is None:
        return DiffSummary(BASELINE_ABSENT, None, None, None)
    if not diff_is_affordable(previous_content, content):
        # Считать нечем — и сказать об этом надо состоянием, а не нулями.
        # Счётчики остаются None по той же причине, что и при
        # ``BASELINE_ABSENT``: «+0/−0» здесь было бы утверждением, что
        # изменений нет, а известно ровно обратное — изменения есть и они
        # велики.
        return DiffSummary(BASELINE_TOO_LARGE, previous_version, None, None)
    # Считать по опкодам, а не по префиксам строк дифа. Разбор «строка
    # начинается с +, но не с +++» разделяет заголовки дифа и содержимое по
    # виду, а вид у них общий: строка текста ``---`` превращается в ``----`` и
    # проходит проверку на заголовок, после чего в счётчик не попадает. Замер:
    # ``hello\n---\nworld`` → ``hello\nworld`` давало «+0/−0» при непустом
    # unified diff — то есть сводка, стоящая ПЕРЕД дифом ровно затем, чтобы
    # отличить правку от переписывания, говорила «правок нет» о настоящей
    # правке (#1169, находка ревью #317). Markdown-разделители и вставленные в
    # текст скилла куски диффов — обычное содержимое, а не редкость.
    #
    # ``SequenceMatcher`` берётся с теми же параметрами, что и внутри
    # ``difflib.unified_diff``, чтобы сводка и показанный под ней диф не
    # разошлись между собой.
    matcher = difflib.SequenceMatcher(
        None, split_lines(previous_content), split_lines(content)
    )
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
    return DiffSummary(BASELINE_VERSION, previous_version, added, removed)


def unified_diff(
    *,
    previous_content: str | None,
    previous_version: int | None,
    content: str,
    version: int,
) -> str:
    """Unified diff к прежней активной версии; пустая строка, если её нет."""
    if previous_content is None:
        return ""
    if not diff_is_affordable(previous_content, content):
        # Тот же потолок, что и у сводки, и тем же вызовом: разойтись им
        # нельзя. Пустая строка здесь не читается как «изменений нет» —
        # рядом стоит сводка с состоянием ``too_large`` и словами о том,
        # почему диф не посчитан.
        return ""
    return "\n".join(
        difflib.unified_diff(
            split_lines(previous_content),
            split_lines(content),
            fromfile=f"v{previous_version}",
            tofile=f"v{version}",
            lineterm="",
        )
    )


def publication_payload(
    *,
    name: str,
    version: int,
    content: str,
    previous_content: str | None,
    previous_version: int | None,
) -> dict[str, object]:
    """Payload события ``skill_activated``, одинаковый на всех трёх путях.

    Существующие ключи ``name`` и ``version`` остаются на месте: читатели
    фида уже опираются на них, а новые поля добавляются рядом. Unified diff в
    событие НЕ кладётся — версии обе лежат в реестре, и текст на 100k
    символов в фиде событий был бы копией того, что и так есть.
    """
    return {
        "name": name,
        "version": version,
        "diff": summarize_change(
            previous_content=previous_content,
            previous_version=previous_version,
            content=content,
        ).as_dict(),
        "content_scan": scan_report(content),
    }
