<!-- Копия seed-скилла machine-review-cycle (#383); источник правды — библиотека скиллов хаба (/skills). -->

# Machine-review cycle (v1) — контракт для любого агента-клиента

Как выполнить machine-review задачи в Haiplane Hub без контекста чужих
сессий. Оркестратор любой (Claude Code Workflow, Cursor, свой скрипт) —
контракт один.

## Когда обязателен
Политика (#382): каскад override задачи > политика проекта (off|auto|always)
> автоправила (docs/chore/spike и размеры XS/S — нет; refactor и
feature/bug M+ — да; риск high или security — всегда). Хаб сам сообщает:
`lifecycle_hint` в ответе `hub_submit_for_review`, предупреждение в панели
ревью, событие `machine_review_requested` в фиде. Режим
HAIPLANE_MACHINE_REVIEW=require блокирует человеческий вердикт без
актуального отчёта; дефолт warn — только предупреждает.

## Шаги
1. `hub_get_skill("multi-agent-review")` — актуальная версия промта-харнесса
   (измерения → пара опровергатель+валидатор на находку → единогласие).
2. Прогнать харнесс над диффом задачи СВОИМ оркестратором: измерения
   параллельно, на каждую находку два независимых верификатора
   («default to refuted»), подтверждение только единогласное.
3. Исправить confirmed-находки, прогнать тесты заново (exit code проверять
   отдельным echo, не через пайп).
4. `hub_submit_machine_review(task_id, raw_count, incomplete,
   findings_confirmed, findings_rejected, unresolved, lost_dimensions,
   harness_skill, harness_version, agent_count, tokens_spent, duration_ms,
   orchestrator, model)`. Метрики опциональны, но токены/время питают экономику
   практики (#384). Отчёт привязывается к текущему submission_generation:
   пересдача работы делает его stale.

   `incomplete` **обязателен и без дефолта** (#549): пропуск даёт 422. Дефолт
   `false` подставлялся бы молча у всех, кто поле забыл, — то есть прогон с
   умершими агентами читался бы как чистый. `unresolved` — находки, которые
   никто не смог рассудить; они НЕ идут в `findings_rejected`, потому что
   «никто не голосовал» и «кто-то опроверг» — противоположные исходы.
   `lost_dimensions` — измерения, не вернувшие результат.
5. `hub_submit_for_review` — человеческий вердикт остаётся финальным гейтом;
   отчёт его информирует, не заменяет.

## Формат находок
confirmed: {title, severity high|medium|low, category slug, file, line,
detail}; rejected: {title, category, reason}; **unresolved: {title, why}**.
category питает метрики повторяемости — используй устойчивые слаги
(security, correctness, consistency, tests, …).

Объяснение у `unresolved` называется **`why`**, а не `reason`: `reason`
принадлежит `findings_rejected`. Имена соседние и легко путаются, поэтому
лишние ключи теперь отвергаются, а не отбрасываются молча (#553) — до этого
перепутанное имя давало сохранённую находку с пустым объяснением, то есть без
того единственного, ради чего её и вынесли на решение человека.
