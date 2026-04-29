#!/usr/bin/env python3
"""One-off: push analyst DoR payloads to Hub roadmap tasks (agenthai.ru)."""

from __future__ import annotations

import os
import sys

import httpx

TOKEN = os.environ.get("OPENCLAW_HUB_TOKEN") or os.environ.get("OPENCLAW_HUB_MCP_TOKEN")
if not TOKEN:
    print("Set OPENCLAW_HUB_TOKEN", file=sys.stderr)
    sys.exit(1)

BASE = os.environ.get("OPENCLAW_HUB_URL", "https://agenthai.ru").rstrip("/")


def ac(_id: str, g: str, w: str, t: str, vb: str = "manual") -> dict:
    return {"id": _id, "given": g, "when": w, "then": t, "verifiable_by": vb}


def risk(kind: str, sev: str, desc: str, mit: str) -> dict:
    return {"kind": kind, "severity": sev, "description": desc, "mitigation": mit}


# --- Epic 11 ---
REFINE_11 = {
    "size": "L",
    "wip_tag": "feature_work",
    "user_story": (
        "Как владелец продукта Hub и как команда разработки мы хотим иметь единый приоритизированный "
        "бэклог улучшений для ИИ-агентов, чтобы снижать трение интеграции и стоимость сопровождения без потери безопасности."
    ),
    "problem_statement": (
        "ИИ-агенты подключаются через MCP и REST, но контракты разъехались: часть операций доступна только через REST, "
        "ответы MCP часто неструктурированы, диагностика ошибок (401/421/406) размазана по чатам. Нет единого места, "
        "где зафиксирован «идеальный» сценарий работы агента с Hub."
    ),
    "business_value": (
        "Быстрее онбординг агентов и людей; меньше инцидентов из-за неверной конфигурации; основа для SLA на автоматизацию."
    ),
    "scope_in": [
        "Согласованный контракт MCP↔REST для типовых операций с задачами",
        "Документация и диагностические сигналы для операторов",
        "Улучшения надёжности создания задач и экономии контекста",
    ],
    "scope_out": [
        "Замена основной модели авторизации Hub целиком",
        "Мультитенантность и биллинг",
    ],
    "technical_hints": (
        "Вести изменения узко: совместимость API, миграции db.py при новых полях, тесты tests/test_mcp_server.py и test_auth."
    ),
    "constraints": ["Обратная совместимость для существующих клиентов MCP/REST"],
    "assumptions": [
        "Основной клиент агента — Streamable HTTP MCP под Bearer",
        "Репозиторий остаётся единственным источником правды для контрактов",
    ],
    "validation_commands": [
        "uv run pytest -q",
        "uv run ruff check hub tests",
    ],
    "review_checklist": [
        "Каждая дочерняя задача имеет проверяемые AC",
        "Риски безопасности явно закрыты или заведены отдельно",
    ],
    "acceptance_criteria": [
        ac(
            "AC-1",
            "Roadmap утверждён (эпик и фичи не в противоречии)",
            "Проведена ревью-сессия с владельцем",
            "Приоритеты и зависимости между задачами зафиксированы в Hub",
        ),
        ac(
            "AC-2",
            "Листовые задачи переведены из draft при необходимости",
            "Человек с ролью human/admin выполняет approve",
            "Ключевые задачи доступны для исполнения без блокирующих пробелов DoR",
        ),
        ac(
            "AC-3",
            "Определён критерий «готово» для эпика на уровне продукта",
            "Релиз или набор merged PR закрывает большинство листьев",
            "В README или docs есть ссылка на то, как подключать Hub после изменений",
        ),
    ],
    "risks": [
        risk(
            "large_scope",
            "medium",
            "Roadmap может разрастись без жёсткого приоритизации спринтов.",
            "Фиксировать MVP на первый релиз (например только MCP parity + docs).",
        ),
    ],
}

# --- Features ---
REFINE_12 = {
    "problem_statement": (
        "MCP инструменты не покрывают полный TaskRefine (AC/risks), ответы tools/call преимущественно текстовые — "
        "агент вынужден парсить строки или вызывать REST."
    ),
    "business_value": (
        "Сокращение времени сценариев «аналитик заполняет задачу» и «агент проверяет успех»; меньше ошибок парсинга."
    ),
    "scope_in": [
        "Паритет полей refine между MCP и REST",
        "Структурированный контракт ответа для ключевых инструментов",
        "Bulk-создание подзадач одним вызовом",
    ],
    "scope_out": [
        "Новый транспорт помимо Streamable HTTP и stdio",
        "OAuth для MCP в этом релизе",
    ],
    "validation_commands": [
        "uv run pytest -q tests/test_mcp_server.py tests/test_auth.py"
    ],
    "acceptance_criteria": [
        ac(
            "AC-1",
            "Разработчик вызывает только MCP",
            "Типовый refine с AC выполнен",
            "Нет обязательного обхода через curl REST",
        ),
        ac(
            "AC-2",
            "Агент парсит ответ",
            "tools/call вернул результат",
            "Есть машиночитаемое поле или стабильная JSON-схема",
        ),
        ac("AC-3", "Регрессия", "pytest MCP/auth", "Все тесты зелёные"),
    ],
}

REFINE_16 = {
    "problem_statement": (
        "Ошибки конфигурации (Accept, URL, токены stdio vs streamable) выявляются методом проб; hub_admin_my_identity не даёт полной картины."
    ),
    "business_value": "Снижение MTTR для поддержки и самообслуживания команд на Cursor/CLI.",
    "scope_in": [
        "Явная диагностика identity и здоровья сервиса",
        "Один канонический гайд для деплоя агента",
    ],
    "scope_out": ["Полноценный observability стек (метрики Prometheus)"],
    "validation_commands": ["Ручная проверка гайда на чистой VM или новом окружении"],
    "acceptance_criteria": [
        ac(
            "AC-1",
            "Новый оператор",
            "Следует гайду с нуля",
            "Поднимает Hub и MCP без обращения к чату",
        ),
        ac(
            "AC-2",
            "Ошибка конфигурации",
            "whoami/health вызываются",
            "Сообщение указывает направление исправления",
        ),
    ],
}

REFINE_19 = {
    "problem_statement": (
        "Повторные запросы агента создают дубликаты задач; большие деревья задач переполняют контекст модели."
    ),
    "business_value": "Предсказуемое состояние бэклога и меньшая стоимость токенов при работе с иерархией.",
    "scope_in": [
        "Идемпотентность или дедуп ключей",
        "Экономный вывод дерева/контекста",
        "Опциональные подсказки следующих шагов",
    ],
    "scope_out": ["Полнотекстовый поиск по задачам в этом эпике"],
    "validation_commands": ["Нагрузочный сценарий: два одинаковых create подряд"],
    "acceptance_criteria": [
        ac(
            "AC-1",
            "Дубликаты",
            "Два запроса с одним ключом",
            "Создана одна сущность или явная идемпотентная ошибка",
        ),
        ac(
            "AC-2",
            "Большое дерево",
            "Запрос context с лимитом",
            "Ответ укладывается в заданный бюджет размера",
        ),
    ],
}

# --- Leaf tasks 13-22 ---
TASKS: dict[int, dict] = {
    13: {
        "user_story": (
            "Как аналитик через MCP я хочу передать acceptance criteria и риски одним вызовом, "
            "чтобы не дублировать данные между MCP и REST."
        ),
        "problem_statement": (
            "`hub_refine_task` не сериализует `acceptance_criteria` и `risks` в тело POST /refine; "
            "агент обязан звать `hub_replace_acceptance_criteria` и `hub_add_risk` отдельно или использовать REST."
        ),
        "business_value": "Меньше шагов и расхождений данных между инструментами; быстрее заполнение DoR из Cursor.",
        "scope_in": [
            "Расширение сигнатуры MCP или новый инструмент-обёртка",
            "Сохранение текущего поведения REST без breaking change",
            "Тесты на паритет полей",
        ],
        "scope_out": ["Изменение схемы SQLite задач без миграции"],
        "technical_hints": (
            "Рассмотреть опциональные параметры `acceptance_criteria: list[dict]` и `risks: list[dict]` в FastMCP "
            "с валидацией как в TaskRefine; альтернатива — `hub_task_define_ready(task_id, patch)` с merge policy."
        ),
        "constraints": ["Не ломать клиентов, завязанных на текущие имена параметров"],
        "assumptions": [
            "Python MCP SDK и FastMCP поддерживают добавление полей к tools"
        ],
        "validation_commands": [
            "uv run pytest -q tests/test_mcp_server.py",
            "ручной tools/call с новым инструментом",
        ],
        "review_checklist": [
            "Документация hub/cli.py и mcp_server.py синхронизированы"
        ],
        "size": "M",
        "wip_tag": "feature_work",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Задача без AC",
                "Аналитик вызывает новый MCP инструмент с AC и рисками",
                "В Hub появились те же AC/риски что и при REST refine",
            ),
            ac(
                "AC-2",
                "Регрессия",
                "Запуск pytest после изменений",
                "Зелёные тесты MCP",
            ),
            ac(
                "AC-3",
                "Обратная совместимость",
                "Старый вызов hub_refine_task только с текстовыми полями",
                "Поведение как сейчас",
            ),
            ac(
                "AC-4",
                "Документация",
                "Обновлён раздел MCP в docs или README",
                "Пример JSON аргументов приложен",
            ),
        ],
        "risks": [
            risk(
                "ambiguous_requirements",
                "medium",
                "Разные интерпретации merge vs replace для списков AC.",
                "Явно описать семантику в docstring инструмента и тестах.",
            ),
        ],
    },
    14: {
        "user_story": (
            "Как агент я хочу создать набор подзадач одним вызовом под родителем, "
            "чтобы декомпозиция не занимала десятки round-trip."
        ),
        "problem_statement": (
            "Нет атомарной операции «массив подзадач»; высокий шум в логах MCP и риск частичного создания при обрыве."
        ),
        "business_value": "Ускорение сценариев планирования и меньше мусорных промежуточных состояний в БД.",
        "scope_in": [
            "Новый endpoint или MCP tool: parent_id + список описаний",
            "Транзакция БД: всё или ничего",
            "Валидация иерархии (subtask под task и т.д.)",
        ],
        "scope_out": ["Параллельное назначение исполнителей и календарь"],
        "technical_hints": (
            "Реализовать в repository транзакцию с множественным INSERT; из MCP вернуть массив id и ошибки валидации по индексу."
        ),
        "constraints": [
            "Соблюдать лимиты размера тела запроса и числа подзадач за вызов"
        ],
        "assumptions": ["Клиент передаёт корректный parent согласно HIERARCHY_RULES"],
        "validation_commands": [
            "uv run pytest -q tests/test_api.py",
            "нагрузочный json с 20 элементами локально",
        ],
        "review_checklist": ["Откат транзакции при первой ошибке валидации"],
        "size": "M",
        "wip_tag": "feature_work",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Валидный родитель",
                "Один вызов с N подзадачами",
                "Создано ровно N записей",
            ),
            ac(
                "AC-2",
                "Невалидная строка i",
                "Один элемент не проходит схему",
                "Ничего не создано, ответ с индексом ошибки",
            ),
            ac(
                "AC-3",
                "MCP",
                "tools/call нового инструмента",
                "Ответ содержит список id в порядке входа",
            ),
        ],
        "risks": [
            risk(
                "breaking_change",
                "low",
                "Увеличение времени удержания блокировок БД на длинных списках.",
                "Лимит N и батчи при необходимости.",
            ),
        ],
    },
    15: {
        "user_story": (
            "Как ИИ-агент я хочу получать результат tools/call в виде структурированных данных, "
            "чтобы программно проверять успех без парсинга текста."
        ),
        "problem_statement": (
            "Сейчас успех часто выражен только во вложенном тексте и JSON внутри строки; хрупко для автоматизации и юнит-проверок."
        ),
        "business_value": "Надёжнее автоматизация и меньше ложных «успехов» при ошибках парсинга.",
        "scope_in": [
            "Определить JSON Schema или MCP structuredContent для классов инструментов",
            "Минимум: hub_create_task, hub_refine_task, hub_task_status",
            "Сохранить текстовое резюме для людей",
        ],
        "scope_out": ["Полная типизация всех 29 инструментов в одном релизе"],
        "technical_hints": (
            "Использовать возможности MCP SDK для structured outputs; версионировать схему полем schema_version."
        ),
        "constraints": [
            "Не увеличивать размер ответа более чем на X% без согласования"
        ],
        "assumptions": ["Cursor и клиенты игнорируют неизвестные поля"],
        "validation_commands": [
            "Contract-тест: сравнение MCP vs REST для одних и тех же операций"
        ],
        "review_checklist": ["Примеры ответов добавлены в документацию"],
        "size": "L",
        "wip_tag": "tech_debt",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Инструмент обновлён",
                "tools/call",
                "Есть блок structured или валидируемый JSON помимо текста",
            ),
            ac(
                "AC-2",
                "Ошибка домена",
                "Намеренно неверный ввод",
                "Структурированный код ошибки, не только текст",
            ),
            ac(
                "AC-3",
                "Документация",
                "Страница для разработчиков агентов",
                "Описана схема и пример",
            ),
        ],
        "risks": [
            risk(
                "ambiguous_requirements",
                "medium",
                "Разные клиенты MCP по-разному отображают structuredContent.",
                "Пилот на Cursor + один CLI-тест.",
            ),
        ],
    },
    17: {
        "user_story": (
            "Как оператор я хочу одним вызовом понять кто я в системе и в каком режиме живёт сервер."
        ),
        "problem_statement": (
            "`hub_admin_my_identity` не различает источник учётных данных и не показывает конфигурацию bind/auth/vast."
        ),
        "business_value": "Быстрее диагностика 401/403 и неправильных ролей.",
        "scope_in": [
            "Расширить или заменить инструмент: username, role, permissions summary",
            "Источник токена: env map vs api_keys row id (без утечки секрета)",
            "health: bind host/port, auth enabled, vast enabled",
        ],
        "scope_out": ["Вывод содержимого secrets.env"],
        "technical_hints": (
            "Читать из config и request.state; для health не выполнять опасных subprocess; версия из hub.__version__ или pyproject."
        ),
        "constraints": ["Не логировать полный Bearer"],
        "assumptions": ["MCP вызывается уже после AuthMiddleware"],
        "validation_commands": [
            "Ручной вызов tools/list и tools/call под разными токенами"
        ],
        "review_checklist": ["Security review на отсутствие PII лишнего"],
        "size": "S",
        "wip_tag": "support",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Env-токен",
                "whoami",
                "Совпадает с ожидаемой ролью из OPENCLAW_HUB_TOKENS",
            ),
            ac(
                "AC-2",
                "DB api key",
                "whoami при ключе из БД",
                "Указано что источник БД без секрета",
            ),
            ac(
                "AC-3",
                "health",
                "Вызов инструмента",
                "bind и флаги auth/vast соответствуют конфигу",
            ),
        ],
        "risks": [
            risk(
                "security",
                "medium",
                "Чрезмерная детализация health раскрывает внутреннюю топологию.",
                "Ограничить вывод уровнем prod-safe whitelist.",
            ),
        ],
    },
    18: {
        "user_story": (
            "Как новый разработчик я хочу один документ от нуля до рабочего MCP URL без разрозненных заметок."
        ),
        "problem_statement": (
            "Знания разбиты между deploy/TAILSCALE.md, ответами в чате и примерами .cursor/mcp.json; частые ошибки Accept и пути /mcp/mcp."
        ),
        "business_value": "Снижение времени онбординга и числа тикетов «не работает MCP».",
        "scope_in": [
            "Канонический URL и заголовки",
            "Различие stdio vs streamable",
            "Таблица ошибок и действий",
            "Пример Cursor и curl",
        ],
        "scope_out": ["Kubernetes helm chart"],
        "technical_hints": "Один файл docs/agent-mcp-operator-guide.md с оглавлением; ссылки из README.",
        "constraints": [
            "Не дублировать полностью admin-agent-deployment-guide — давать перекрёстные ссылки"
        ],
        "assumptions": ["Аудитория: оператор с SSH и nginx"],
        "validation_commands": ["Peer review вторым человеком по чек-листу из AC"],
        "review_checklist": ["Все curl-примеры проверены на staging"],
        "size": "S",
        "wip_tag": "tech_debt",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Новый читатель",
                "Проходит раздел от нуля",
                "Получает рабочий MCP хотя бы на staging",
            ),
            ac(
                "AC-2",
                "Ошибки",
                "Раздел troubleshooting",
                "Покрыты 401, 421, 406 и Missing session",
            ),
            ac(
                "AC-3",
                "Индекс",
                "README ссылается на документ",
                "Ссылка открывается в репозитории",
            ),
        ],
        "risks": [],
    },
    20: {
        "user_story": (
            "Как агент я хочу безопасно повторить создание задачи после таймаута, не создавая дубликатов."
        ),
        "problem_statement": (
            "Идемпотентность отсутствует; повтор POST создаёт вторую задачу с тем же смыслом."
        ),
        "business_value": "Чистый бэклог и меньше ручной уборки дублей.",
        "scope_in": [
            "Дизайн ключа идемпотентности (header или body)",
            "Хранение обработанных ключей с TTL или уникальный индекс",
            "Ответ 409/200 с существующим id",
        ],
        "scope_out": ["Идемпотентность для всех PATCH методов"],
        "technical_hints": (
            "Таблица idempotency_keys(task_hash unique) или колонка client_request_id unique nullable с очисткой cron."
        ),
        "constraints": [
            "Не хранить полный текст задачи в ключе если конфиденциально — использовать hash"
        ],
        "assumptions": ["Клиент генерирует UUID запроса"],
        "validation_commands": ["Тест: два одинаковых запроса подряд — один id"],
        "review_checklist": ["Миграция в db.py с тестом"],
        "size": "M",
        "wip_tag": "tech_debt",
        "acceptance_criteria": [
            ac("AC-1", "Первый запрос", "POST с ключом", "201 и новый id"),
            ac("AC-2", "Повтор", "Тот же ключ", "Тот же id без второй строки в tasks"),
            ac(
                "AC-3",
                "Конфликт смысла",
                "Тот же ключ но другой payload",
                "Ошибка 409 с понятным телом",
            ),
        ],
        "risks": [
            risk(
                "data_migration",
                "low",
                "Миграция и размер таблицы ключей.",
                "TTL и индекс по expires_at.",
            ),
        ],
    },
    21: {
        "user_story": (
            "Как агент с ограниченным контекстом я хочу получать короткое дерево задач по умолчанию."
        ),
        "problem_statement": (
            "`hub_task_tree` и `hub_my_context` могут вернуть очень большие строки на глубоких эпиках."
        ),
        "business_value": "Экономия токенов и стабильнее поведение модели.",
        "scope_in": [
            "Параметры depth/max_nodes/max_chars с разумными дефолтами",
            "Режим summary vs full",
            "Совместимость: без параметров — как сейчас или мягкий лимит",
        ],
        "scope_out": ["Стриминг дерева по частям"],
        "technical_hints": (
            "Обрезка на уровне сервиса после сборки дерева; счётчик символов UTF-8."
        ),
        "constraints": ["Не скрывать факт обрезки — пометка «truncated» в тексте"],
        "assumptions": ["Клиенты MCP передают числовые аргументы"],
        "validation_commands": ["Юнит-тест на искусственно глубокое дерево"],
        "review_checklist": ["Документировать дефолты в docstring инструмента"],
        "size": "S",
        "wip_tag": "feature_work",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Глубокое дерево",
                "Вызов с depth=2",
                "Нет узлов глубже 2 от корня",
            ),
            ac(
                "AC-2",
                "Лимит символов",
                "max_chars маленький",
                "В конце есть уведомление truncated",
            ),
            ac(
                "AC-3",
                "Обратная совместимость",
                "Вызов без параметров",
                "Поведение документировано и не ломает старые клиенты",
            ),
        ],
        "risks": [
            risk(
                "performance",
                "low",
                "Дополнительные проходы по дереву.",
                "Ранний выход при достижении лимита.",
            ),
        ],
    },
    22: {
        "user_story": (
            "Как аналитик я хочу после readiness видеть конкретные следующие шаги, а не только числовой score."
        ),
        "problem_statement": (
            "Агент вручную интерпретирует readiness JSON; нет стандартизированного «что делать дальше»."
        ),
        "business_value": "Ускорение цикла «проверил DoR → дополнил поля → снова readiness».",
        "scope_in": [
            "Новый инструмент или расширение hub_get_readiness",
            "Генерация 3–5 шагов из blocking recommendations",
            "Формат: markdown или structured list",
        ],
        "scope_out": ["Автоисправление полей без человека"],
        "technical_hints": (
            "Переиспользовать сервис readiness report; не вызывать LLM — только детерминированный маппинг полей."
        ),
        "constraints": ["Не выполнять побочных действий с задачей"],
        "assumptions": ["Readiness уже содержит recommendations"],
        "validation_commands": ["Сравнить вывод с explain=true для одной задачи"],
        "review_checklist": [
            "Проверить на задаче без блокеров — пустой список или общий совет"
        ],
        "size": "S",
        "wip_tag": "feature_work",
        "acceptance_criteria": [
            ac(
                "AC-1",
                "Задача с блокерами",
                "Вызов инструмента",
                "Минимум три шага совпадают с топ-блокерами",
            ),
            ac(
                "AC-2",
                "Задача готова",
                "dor_passed true",
                "Нет ложных блокирующих шагов",
            ),
            ac("AC-3", "Тесты", "pytest сервисов readiness", "Зелёные"),
        ],
        "risks": [
            risk(
                "ambiguous_requirements",
                "low",
                "Шаги могут дублировать текст readiness.",
                "Явный формат bullet и дедупликация по полю.",
            ),
        ],
    },
}


def main() -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    url = f"{BASE}/api/tasks/{{}}/refine"

    bundles = [
        (11, REFINE_11),
        (12, REFINE_12),
        (16, REFINE_16),
        (19, REFINE_19),
    ] + list(TASKS.items())

    with httpx.Client(timeout=120.0) as client:
        for tid, body in bundles:
            r = client.post(url.format(tid), headers=headers, json=body)
            if r.status_code != 200:
                print(f"FAIL #{tid} HTTP {r.status_code}: {r.text[:400]}")
                sys.exit(1)
            d = r.json()
            print(
                f"OK #{tid} readiness_score={d.get('readiness_score')} dor_passed={d.get('dor_passed')}"
            )

    print("All refine calls succeeded.")


if __name__ == "__main__":
    main()
