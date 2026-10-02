---
name: hub-task-prep
description: Use when turning a request into a structured Haiplane Hub task pack with work type selection, scope, acceptance criteria, risks, readiness expectations, and validation commands.
---

# Hub Task Prep

## Use This Skill For

- drafting or refining Hub tasks
- choosing a work type template
- preparing acceptance criteria and risks
- improving readiness before approval

## Workflow

1. Pick the closest template in `hub/cli_templates/work_types/`.
2. Check the required readiness fields in `hub/services/dor.py`.
3. Use `hub/models.py` as the schema source of truth.
4. Write scope in and scope out so implementation boundaries are unambiguous.
5. Make every acceptance criterion observable and verifiable.

## Проверка утверждений

Каждое фактическое утверждение постановки (путь, имя, строка, число, «уже
делает») относится к одному из семи классов, и у класса своя проверка. Текст
постановки фактом не является: в замере 28.09–01.10 это были правки постановки
после чтения кода, а не после ревью. Примеры ниже взяты из карточек этих задач.

| Класс | Проверка | Пример из замера |
|---|---|---|
| Путь | `git cat-file -e origin/<база>:<путь>`; нет такого — пометка «новый» и путь в `affected_areas`. Имя существующей команды проверяется так же по базе: читается `[project.scripts]` в `pyproject.toml` | #1392 (SID-17): validation-команда указывала на `tests/fixtures/home_basic/.ssh`, каталога нет, фикстура плоская; `git cat-file -e` на базе ловит это до одобрения, без неё агент подменил путь в `cli.py`. Имя команды: #1515 писала `oc-hub worktree`, в `pyproject.toml` скрипт `hp-hub` (правка 01.10) |
| Функция или строка кода | функция найдена на базе по имени (`git show origin/<база>:<путь> \| grep -n "def <имя>"`), в постановке — `path:line`, имя функции и sha базы, на которой строка снята | #1500: ссылка `hub/app.py:2211` уехала; на `origin/develop` 8f23edf (02.10) `api_review_verdict` стоит на `hub/app.py:2277`. Номер строки плавает, имя функции держится: проверка находит функцию по имени и пишет актуальную строку с sha |
| Поле, колонка или событие | запрос к данным или чтение места записи, с датой и sha базы | #1500: `actor` вердиктов на проде 30.09 пяти видов, колонки принципала у событий нет. Код на `origin/develop` 8f23edf: `review_verdict_recorded` пишется в `hub/services/lifecycle.py` (около :4079) с payload `{verdict, submission_generation, self_approved}`; `author_kind` есть колонкой `task_updates` (`hub/db.py:759`), в payload событий его нет. Постановка вводит его как новое поле, а не читает существующее; проверка — прочитать место записи и схему |
| «X уже делает Y» | прочитан код X, а не его описание | #1503: «отказ сдачи из-за непушнутой ветки» не существует; в `lifecycle` его нет (проверено 30.09), пустой `submission_sha` принимается. Класс сбоя определяется по результату, а не по отказу |
| Данные и счётчики | запрос с датой и источником; «около N» не пишется | #1446: «прогон завёл хаб, значит видит» верно только для прогонов хаба; SID-12 и SID-15 запускались мимо хаба, в `executor_runs` их нет. Запрос к `executor_runs` по двум задачам разделяет случаи |
| Имена новых сущностей | поиск имени по коду, MCP-каталогу и соседним службам: не занято | #1502: имя `decisions` для очереди действий человека отвергнуто как занятое (`hub_list_decisions` уже есть: `hub/mcp_server.py:4450` на `origin/develop` 8f23edf). После переименования остаток прежнего имени остался в `outcome_metric` карточки (`/api/decisions/pending`): грепни всю карточку, не только заголовок |
| Области | `affected_areas` сверены со всеми `open` и `running` задачами (`hub_list_tasks`) | #1501: пересечение с #1477 по функции порядка названо в тексте, #1527 стоит в зависимостях. #1503 пересекается с #1446 и #1458 по `executor_dispatch.py` |

Как пользоваться:

1. Выпиши утверждения постановки списком и отнеси каждое к классу.
2. Рядом с утверждением запиши проверку и её результат с датой. Нет проверки —
   утверждение уходит в `assumptions`, а не в описание как факт.
3. Расхождение правит текст постановки, а не код под текст.

### Круг «как критик»

После записи черновик проходит один круг «как критик» с инструментами, а не
перечитыванием: по каждой строке списка из шага 1 запускается команда класса
(`git cat-file`, `grep -n`, запрос, чтение кода X, `hub_list_tasks`). Перечитать
свой текст глазами кругом не считается. Результат круга — правки постановки и
отметка в самой карточке, что проверено и когда. Второго круга по умолчанию нет.

## Output Requirements

- clear `work_type`
- explicit `scope_in`
- acceptance criteria with a validation path
- realistic validation commands
- risks with mitigation when uncertainty exists

## Reference Files

- `hub/models.py`
- `hub/services/dor.py`
- `hub/services/recommendations.py`
- `hub/cli_templates/work_types/`
