---
name: hub-submit-task
description: Use before hub_submit_for_review on a Haiplane Hub pair task - payload shape (prevention.ref, mutations, finding_outcomes), what a resubmission does to the generation and the verdict, what to check after a transport error, and what must be pushed and green before the submission.
---

# Hub Submit Task

Каждая сдача с новым sha открывает новое поколение и покупает ревью. Этот skill
перечисляет шесть правил, которые держат число сдач и отказов по форме
минимальными. Парный skill для чтения отчёта после сдачи:
`skills/hub-review-report-reading/`. Общий цикл pair-задачи и дисциплина
исполнителя — skill `executor-pair-discipline` в библиотеке хаба; здесь он не
копируется.

Ссылки на код даны по имени сущности на базе `1b60f5453fccfe9e59a1745666b084ca13c36a67`
(origin/develop). Номеров строк нет: ищите сущность по имени.

## 1. Вывод по прод-дефекту идёт в `prevention.ref`, не в `test_ref`

- Поля `DefectPrevention` (`hub/models.py`): `kind`, `ref`, `reason`, `revisit`.
  Поля `test_ref` там нет: `test_ref` принадлежит критерию приёмки (AC), не выводу.
- `DefectPrevention` не запрещает лишние поля (в отличие от `FindingOutcomeItem`,
  где `extra="forbid"`). Присланный `test_ref` отбрасывается молча, `ref`
  остаётся пустым, и `prevention_gate.validate_prevention` отвечает 422
  `regression_test без ref`. Проверено запуском модели на этой базе.
- Три вида вывода (`PreventionKind`, `prevention_gate.PREVENTION_OPTIONS`):
  - `regression_test`: `ref` = локатор теста `tests/x.py::test_y`;
  - `rule`: `ref` = категория, которая уже есть в `category_checks`; иначе 422
    `prevention_invalid` (запись категории - `POST /api/metrics/category-checks`);
  - `accepted_risk`: `reason` и `revisit` оба обязательны, `ref` не нужен.
- Когда обязателен: у задачи `found_in='prod'` сдача без `prevention` получает
  422 `prevention_required` (`prevention_gate.check_submission`,
  `refuse_without_prevention`). Если `prevention` прислан, он проверяется всегда.
- Пример payload: `prevention={"kind": "regression_test", "ref": "tests/test_x.py::test_y"}`.
- Оговорка: тексты отказов в `prevention_gate` до сих пор называют CLI `oc-hub`.
  Имя команды - `hp-hub` (правило 6).
- Инцидент не привязан: отказ 422 по `ref`/`test_ref` в ленте #1600 не найден
  (запись 10676, отказы по форме payload в ленту не пишутся). Правило держится
  на коде, не на инциденте.

## 2. Комментарий, повтор того же sha и новый sha - три разные вещи

Источник: `lifecycle.submit_for_review` и `_same_sha_noop_response`.

| Что вы сделали | Что происходит |
| --- | --- |
| Запись в ленту (`hub_task_update`, kind `status`) | Поколение, sha и вердикт не меняются. Это комментарий, не сдача. |
| `hub_submit_for_review` из `review`, ветка на том же sha | Не новое поколение (#1265). Статус, поколение, `submission_sha` и текущесть вердикта остаются прежними. Данные сдачи (`finding_outcomes`, `accept_areas`) применяются к текущему поколению; решение о заказе ревью принимается заново, и если полного отчёта нет, прогон может быть заказан на том же поколении. Ветку этот путь не пушит и PR не открывает. |
| `hub_submit_for_review` из `review`, ветка на новом sha | Новое поколение (#1054). `repository.bump_submission_generation` повышает счётчик, прежний вердикт перестаёт быть текущим (вердикт привязан к поколению), задача остаётся в `review`. В ленту пишется «Пересдача из review: сдача X заменена на Y». |
| `hub_submit_for_review` из `running` | Обычная сдача, новое поколение. |

Следствия:

- Правка после сдачи требует нового sha и новой сдачи; поправить сданный код
  записью в ленту нельзя. Старая фраза «правки в review - апдейтом» неверна после
  #1054 и не используется.
- sha закрепляет хаб, не клиент: `lifecycle.resolve_branch_tip` читает
  `origin/<branch>` на момент сдачи. Пуш после сдачи ничего не меняет, пока нет
  пересдачи.
- Пересдача без изменений ничего не даёт; пересдача ради каждой находки по
  отдельности покупает ревью каждый раз. Собирайте правки в один коммит.
- Журнал #1600 (запись 10676): три сдачи, три разных sha (97788de, f0d96ad,
  03999db), все три - новые поколения; повторов того же sha не было.

## 3. Ошибка транспорта не значит, что сдача не записана

- Сначала `hub_task_status(task_id)`: поля `status`, `submission_generation`,
  `submission_sha`, `latest_review`. Сдача записана, если статус `review` и
  поколение выросло против того, что было до вызова.
- Клиентский `hub_submit_for_review` сам читает задачу до вызова
  (`prior_status`, `prior_generation`) и по совпадению поколения узнаёт повтор
  того же sha (`was_unchanged_retry`); других ключей идемпотентности у сдачи нет.
- Слепой повтор после «упавшего» вызова либо будет no-op (тот же sha из `review`,
  правило 2), либо, если первый вызов не дошёл, станет первой сдачей. Тот и другой
  исход читается по `hub_task_status`, не по тексту ошибки.
- Источник правила - практика сессий (запись памяти о цене повтора); в коде
  подтверждено только наличие полей сверки и поведение повтора, описанное выше.

## 4. `mutations` - список `{ac, mutation, failed_test}`

- Формат: `[{"ac": "AC-1", "mutation": "что сломано в коде", "failed_test": "<test_ref этого AC>"}]`
  (`models.SubmissionMutation`, `submission_contract.MUTATIONS_FORMAT`).
- По записи на каждый AC с `verifiable_by=test`. `failed_test` обязан совпасть с
  `test_ref` именно этого AC; AC без `test_ref` требует непустого `failed_test`
  (`submission_contract`, проверки мутаций). Неизвестный `ac` и пустое
  `mutation` - нарушения.
- Обязательность задаёт политика проекта `submission_contract`: `warn` пишет
  нарушение в карточку, `require` отказывает 422 (#1436). Прислать поле стоит и
  при `off`.
- Это заявление, не наблюдение: хаб мутаций не исполняет. Мутацию нужно
  действительно сделать и увидеть красный тест, иначе запись ложная.
- Для `bug_red_test` мутации не доказательство: красный базовый прогон CI
  проверяется отдельно (#913, `red_test_gate`).

## 5. До сдачи: пуш, CI нужного sha, критик на ядре

1. Коммит сделан, дерево чистое. Хук `.githooks/pre-push` отказывает в пуше с
   грязным деревом и из ветки с именем вне `task-*/*`, `fix/*`, `chore/*`,
   `docs/*`, `ci/*`, `dependabot/*`.
2. Пуш полным refspec: `git push origin HEAD:refs/heads/<task-N/slug>`; затем
   `git ls-remote origin <ветка>` и сверка sha с `git rev-parse HEAD`. Хаб
   читает `origin/<ветка>`, и сдача закрепляет именно то, что там лежит
   (`resolve_branch_tip`).
3. CI зелёный на этом sha. Хаб подхватывает отчёт прогона CI для закреплённого
   коммита (`adopt_ci_run_report`), поэтому прогон нужного sha должен успеть
   закончиться и быть зелёным до сдачи.
4. На ядре хаба (lifecycle, схема, DoR, интеграции) сначала критик Codex, правка
   P1/P2 одним кругом, затем одна сдача. Источник: правило владельца от 06.10
   (постановка #1612); это правило процесса, кодом хаба оно не проверяется. Запуск:
   `codex exec` в режиме read-only со stdin, закрытым `< /dev/null`.
5. Локальные проверки до сдачи: `make lint types budget security` и `uv run pytest -q`
   смотрите по коду возврата, не по хвосту вывода.

## 6. CLI называется `hp-hub`

- `pyproject.toml`, `[project.scripts]`: `hp-hub = "hub.cli:main"`. Скрипта
  `oc-hub` в этой базе нет. Сдача из командной строки: `hp-hub submit-review <id>`
  с `--prevention '<json>'` (`hub.cli.cmd_submit_review`).
- Часть текстов отказов хаба и старых заметок ещё говорит `oc-hub`: читайте это
  как `hp-hub`.
