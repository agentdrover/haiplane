# ADR 0002: MCP/CLI tool consolidation and alias deprecation plan

| Field | Value |
|-------|-------|
| Status | **Proposed** |
| Date | 2026-07-11 |
| Hub | Task [#256](https://agenthai.ru/tasks/256) — Epic #243, F4 |
| Deciders | Hub maintainers |

## Context

Поверхность MCP выросла до 40+ инструментов; часть из них — исторические
алиасы или почти-дубликаты. Агенты путаются в выборе, контрактные тесты
дублируются, а плана вывода алиасов не существует.

## Инвентаризация пересечений

| Инструмент | Пересекается с | Классификация | Решение |
|---|---|---|---|
| `hub_task_update kind=done` | `hub_report_done` | **deprecated alias** (задокументирован) | вывести по этапам |
| `hub_approve_proposal` | `hub_approve_task` | **deprecated alias** | вывести по этапам |
| `hub_reject_proposal` | `hub_reject_task` | **deprecated alias** | вывести по этапам |
| `hub_task_update kind=review` | `hub_submit_review` | **legacy-канал вердикта** (поллер сканирует текст) | мигрировать headless-ревьюера на `hub_submit_review`, скан оставить fallback-ом для dispatch-логов |
| `hub_refine_task`, `hub_add/upsert/replace/delete_acceptance_criterion`, `hub_add_risk` | `hub_prepare_developer_task` | **granular vs macro** — НЕ дубликаты | оставить оба слоя: granular = API-паритет, prepare = аналитический макрос; в описаниях указать «prepare предпочтителен для полного hand-off» |
| `hub_list_proposals` | `hub_list_tasks(status=draft)` | **filter view** с добавленной ценностью (ранжирование #253, ready_to_approve) | оставить; описание уточнить как «ranked draft queue» |
| `hub_start_task` vs `hub_pair_start` | — | разные операции (headless dispatch vs pair) | оставить |
| `hub_force_complete_task` vs `hub_decide_task` | — | разные human-гейты | оставить |

Итог: настоящих кандидатов на удаление три — алиасы `task_update kind=done`,
`approve_proposal`, `reject_proposal`; плюс одна миграция канала вердикта.

## Этапы депрекации

1. **Stage 1 — warning + учёт.** Ответ алиаса получает поле
   `deprecated: true` и `next_action` с указанием замены; каждый вызов
   алиаса пишется в `activity_log` (`kind=deprecated_tool_call`).
   Критерий выхода: телеметрия работает.
2. **Stage 2 — soft-off.** Алиасы возвращают структурированную ошибку
   `reason=tool_deprecated` (hint → замена), если не выставлен
   `OPENCLAW_ALLOW_DEPRECATED_TOOLS=1`.
   Критерий входа: 0 вызовов алиаса в activity_log за 30 дней ИЛИ явное
   решение владельца.
3. **Stage 3 — удаление.** Код, тесты и упоминания в докax удаляются;
   запись в release notes.

Владелец решений по этапам: human owner хаба (Denis Pukinov). Переход
между этапами — отдельные задачи через human-гейт.

## Последствия

- Агентские правила (#310) не меняются — они уже указывают канонические
  инструменты.
- Контрактные тесты алиасов живут до Stage 3.
- `hub_task_update` как инструмент остаётся (status/blocker/question —
  легитимные kinds); депрекация касается только `kind=done` и роли
  `kind=review` как канала вердикта.
