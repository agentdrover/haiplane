---
name: hub-review-report-reading
description: Use after submitting a Haiplane Hub task for review, when reading the machine review report - when to wait, why an incomplete report is not clean, where finding_uid comes from, and how to answer findings with finding_outcomes.
---

# Hub Review Report Reading

Четыре правила чтения отчёта ревью после `hub_submit_for_review`. Парный skill
о сдаче: `skills/hub-submit-task/`. Дисциплина исполнителя в целом - skill
`executor-pair-discipline` в библиотеке хаба; здесь он не копируется.

Ссылки на код даны по имени сущности на базе `1b60f5453fccfe9e59a1745666b084ca13c36a67`
(origin/develop). Номеров строк нет.

## 1. Ждать по состоянию рана и поколению, не по секундам

- Единица ожидания - поколение сдачи (`tasks.submission_generation`), не время.
  Отчёт другого поколения не ревью вашей сдачи: `review_evidence.report_view`
  помечает его `stale`.
- Состояние рана читает `review_evidence.review_wait_view`: `pending` (ревью
  положено и может прийти) или `terminal` с причиной: `report_ready`,
  `review_not_requested`, `not_dispatchable`, `failed_without_ask_again`,
  `finished_without_report`, а также отказы диспетчера. Этот читатель обслуживает
  стюарда; исполнителю доступна та же правда через бриф и карточку:
  `review_in_flight` (заказ активен: модель, профиль, минуты, grace-срок) и
  `current_generation_review` (`GenerationReview`: `has_review`, `reason`,
  `headline`; причины `in_flight`, `incomplete_report`, `no_execution_evidence`,
  `run_failed`, `provider_refused`, `not_dispatched`).
- Первая пришедшая строка отчёта не итог. Ревьюер может прислать второй отчёт
  на то же поколение, и первый его не заменяет: обе строки принимаются
  (`machine_review_intake._retract_no_candidates_alert`, #1584).
  `REPORT_BLOCK_INSTRUCTION` требует от рана ровно одну финальную сдачу после
  прохода и запрещает раннюю, потому что хаб закрывает заказ по первому отчёту.
- Исчезновение `review_in_flight` и `terminal`/`report_ready` из
  `review_wait_view` финальность НЕ доказывают. Свип облачных заказов
  (`review_dispatch`, обход активных заказов) ставит заказу `done`, как только
  находит отчёт принципала-ревьюера по этому поколению (`_dispatch_report`), и
  делает это до проверки состояния рана у провайдера. Статус рана
  (`_TERMINAL_RUN_STATUSES`: FINISHED, ERROR, CANCELLED, EXPIRED) читает только
  сам свип через `cursor_cloud.get_run`; бриф и карточка исполнителю его не
  отдают.
- Надёжного признака, что ран окончен и второго отчёта не будет, у исполнителя
  в коде нет. Что доступно: `id` отчёта (`MachineReviewView.id`) и список отчётов
  поколения; новый отчёт появляется как новая строка с большим `id`. Неизменность
  `id` при нескольких чтениях подряд говорит лишь, что нового отчёта пока нет.
  Докладывайте так: «на момент чтения у поколения N отчёт #id, заказ закрыт/не
  закрыт; ран мог не закончиться». Не пишите «ревью завершено» и «чисто» по
  одному чтению.
- Фиксированной паузы (например, 90 с) правило не содержит: она не привязана ни
  к ране, ни к поколению. `review_in_flight.grace_until` - не срок окончания
  ожидания: это порог, после которого свип разбирает ран, уже завершившийся без
  отчёта (`review_dispatch`, проверка `CURSOR_REVIEW_GRACE_MINUTES`); работающий
  ран остаётся активным и после этого порога.
- Пересдача нового sha открывает новое поколение и обнуляет ожидание
  (`hub-submit-task`, правило 2).

## 2. `incomplete` с нулём находок - не «чисто»

- Отчёт на лестнице исходов (`steward_corridor.report_outcome`) стоит на одной из
  ступеней: `incomplete`, `confirmed`, `unresolved`, `no_data`, `clean`. Чисто -
  только `clean` (`names_clean`): отчёт не неполный, находок нет, и
  `raw_count >= 1`.
- `incomplete=true` - это «не проверено», при любом числе находок.
  `raw_count=0` без неполноты - `no_data`, «кандидатов не было», тоже не чисто.
  Неразрешённые находки - не чисто: никто не смог их рассудить.
- Причина неполноты - поле `incomplete_reason` (`models.INCOMPLETE_REASONS`):
  - `environment`: смотреть было нечем (нет чем запустить проверки, не
    разрешается база сравнения); лечится окружением, второй прогон не лечит;
  - `profile`: инструменты были, охвата профиля не хватило; лечится ещё одним
    прогоном или более широким профилем;
  - пусто: причина не заявлена (`normalise_incomplete_reason` сводит любое
    чужое слово к пустой); хаб причину по тексту не угадывает.
- Что докладывать: «ревью неполное, причина environment/profile/не заявлена»,
  не «находок нет». `GenerationReview.has_review` описывает поколение, не
  отдельный отчёт: оно истинно, если среди отчётов поколения есть полный и с
  уликой исполнения (`review_availability._is_review`), даже когда рядом лежит
  неполный. `reason=incomplete_report` ставится, только когда полного отчёта у
  поколения нет (`_absence_reason`). Поэтому смотрите на конкретный отчёт и его
  `incomplete`, а не только на `has_review`.

## 3. `finding_uid` берётся из брифа или из alert сдачи

- `finding_uid` хаб выводит из содержимого находки, свой придумать нельзя:
  присланный в отчёте отвергается с 422. Алгоритмов два:
  - подтверждённые (`finding_identity.finding_uid`): хеш категории, файла,
    нормализованного заголовка и места; дубликат внутри отчёта получает
    порядковый номер;
  - неразрешённые (`finding_identity.unresolved_uid`): отдельное пространство
    имён `unresolved`, в хеш входит только нормализованный заголовок (`why` не
    входит), дубликат тоже различается порядковым номером.
- Где его читать:
  - в брифе: `ReviewBrief.machine_review`, у каждой находки в
    `findings_confirmed` и записи в `unresolved` (`MachineReviewView`
    штампует uid при каждом чтении);
  - в сообщении хаба о долге по исходам: `finding_outcome.refusal_text` при
    режиме `require` (422) и `warn_note` при `warn` (alert «Отчёт проверок на
    сдаче» в ленте) называют находки с `(uid ...)`.
- Адрес, не найденный среди открытых находок поколения, - 422 (опечатка не
  считается частичным успехом). Перепечатывайте uid копированием, не по памяти.
- Переформулированный заголовок или сдвиг строки - уже другой uid: после новой
  сдачи берите uid из нового отчёта.

## 4. `finding_outcomes` и два словаря исходов

Формат элемента (`models.FindingOutcomeItem`, лишние поля запрещены):
`{finding_uid, outcome, note?, linked_task_id?}`. Идёт в
`hub_submit_for_review(finding_outcomes=[...])`; тот же список принимает
`hub_report_done`. Отвечает автор в сдаче, которая закрывает находки
предыдущей.

Два словаря (`models.FindingOutcome`, `CONFIRMED_OUTCOMES`, `UNRESOLVED_OUTCOMES`):

| Раздел отчёта | Допустимые исходы |
| --- | --- |
| подтверждённые (`findings_confirmed`) | `fixed`, `false_positive`, `wont_fix`, `deferred` |
| неразрешённые (`unresolved`, автор судит сам) | `real_fixed`, `real_deferred`, `not_a_defect`, `not_judged` |

- Слово чужого словаря хаб отклоняет вслух (`_refuse_foreign_dictionary`):
  `false_positive` про неразрешённую находку записало бы, что гейт её
  подтвердил.
- `note` обязателен для всех исходов, кроме `fixed` и `real_fixed`
  (`_SELF_EVIDENT_OUTCOMES`): исправление видно в диффе, остальное - только
  в вашем слове. Пустой `note` - ошибка валидации.
- Исходы, которые оставляют дефект в коде (`wont_fix`, `deferred`,
  `real_deferred`, `not_judged`; `OUTCOMES_LEAVING_WORK`), порождают дефект-драфт;
  `linked_task_id` связывает находку с уже заведённой задачей.
- Режим гейта (`config.FINDING_OUTCOME`, по умолчанию `warn`) и его границы.
  Для обычной новой pair-сдачи (`submit_for_review` с новым sha): при `require`
  сдача без ответа на каждую открытую находку отказывается 422, при `warn`
  принимается с alert. Для done-отчёта (`DONE_OUTCOME_STEPS`) режим ограничен
  потолком `warn`. Повтор того же sha из `review` (`_same_sha_noop_response`)
  записывает переданные исходы и отказывает 422 только на неизвестный uid, а
  оставшиеся открытыми находки не проверяет. Ответ на каждый открытый uid давайте
  в любом случае.
- Один ответ на uid закрывает дефект во всех отчётах поколения
  (`finding_outcome.plan_outcomes`).
