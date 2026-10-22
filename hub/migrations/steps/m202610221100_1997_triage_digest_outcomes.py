"""#1997: учёт исхода разбора k8 - связь «исход -> дайджест» вместо id в JSON.

``triage_digest_outcomes.recommendation_id`` - PRIMARY KEY: исход учтён не более
одного раза за всю жизнь, и это держит сама база, а не проверка перед вставкой.
``kind`` - ``day`` (действие в сутках дайджеста) или ``late`` (опоздавший исход).
Связь пишется в той же транзакции, что и INSERT дайджеста (digest.py), поэтому
дайджест без связей и связь без дайджеста невозможны. Внешних ключей нет
намеренно: таблицу рекомендаций пересобирала миграция #1947, а удалённый
дайджест не должен возвращать исход в счёт.

Заполнение старых данных: связь создаётся ТОЛЬКО для доказуемо учтённого исхода -
его id есть в ``triage.outcome_ids`` измеренного (``state = measured``) дайджеста
и рекомендация существует. ``kind`` - ``late``, если действие исхода раньше суток
дайджеста, иначе ``day``. Дайджест без ``outcome_ids`` (до круга 2 #1943) или
нечитаемый ничего не подтверждает; нечитаемый JSON пропускается. Исход в списках
двух дайджестов достаётся более раннему (INSERT OR IGNORE, порядок по id).
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "create_triage_digest_outcomes",
        """CREATE TABLE IF NOT EXISTS triage_digest_outcomes (
            recommendation_id INTEGER PRIMARY KEY,
            digest_id         INTEGER NOT NULL,
            kind              TEXT    NOT NULL CHECK (kind IN ('day', 'late')),
            created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "backfill_triage_digest_outcomes",
        "INSERT OR IGNORE INTO triage_digest_outcomes "
        "(recommendation_id, digest_id, kind) "
        "SELECT r.id, d.id, CASE WHEN COALESCE(r.followup_action_at, r.followup_at) "
        "< d.digest_date || ' 00:00:00' THEN 'late' ELSE 'day' END "
        "FROM autopilot_digests d, "
        "json_each(CASE WHEN json_valid(d.payload) THEN d.payload ELSE '{}' END, "
        "'$.triage.outcome_ids') j "
        "JOIN executor_triage_recommendations r ON r.id = j.value "
        "WHERE json_extract(CASE WHEN json_valid(d.payload) THEN d.payload "
        "ELSE '{}' END, '$.triage.state') = 'measured' "
        "AND j.type = 'integer' "
        "ORDER BY d.id",
    ),
]
