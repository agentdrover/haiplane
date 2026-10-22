"""#1997: учёт исхода разбора k8 - связь «исход -> дайджест» вместо id в JSON.

``triage_digest_outcomes.recommendation_id`` - PRIMARY KEY: исход известен и
учтён не более одного раза за всю жизнь, и это держит сама база. Строка - ОДНО
состояние исхода: ``digest_id IS NULL`` - исход записан, но ещё не вошёл ни в один
дайджест (очередь), иначе ``digest_id`` и ``kind`` (``day`` - действие в сутках
дайджеста, ``late`` - опоздавший). Очередь индексируется частичным индексом, поэтому
выбор неучтённых идёт за числом НЕучтённых, а не за длиной истории.

Строку очереди заводит ТРИГГЕР при записи ``followup`` (INSERT или UPDATE
рекомендации), единственный писатель очереди: путей записи исхода несколько
(свип, тесты, ручные правки), и триггер покрывает все. Учёт закрывает ту же строку
(UPDATE в транзакции INSERT дайджеста, digest.py). Триггеры принадлежат таблице
рекомендаций: пересборка этой таблицы (как в #1947) должна пересоздать и их.
Внешних ключей нет намеренно: удалённый дайджест не должен возвращать исход в
счёт.

Удаление рекомендации (в том числе каскадом) снимает её строку из очереди; учтённая
строка остаётся историей.

Заполнение старых данных: учтёнными (``digest_id`` заполнен) становятся ТОЛЬКО
доказуемо учтённые - id есть в ``triage.outcome_ids`` измеренного
(``state = measured``) дайджеста и рекомендация существует; ``kind`` - ``late``,
если действие исхода раньше суток дайджеста, иначе ``day``. Дайджест без
``outcome_ids`` (до круга 2 #1943) или нечитаемый ничего не подтверждает; нечитаемый
JSON пропускается; исход из списков двух дайджестов достаётся более раннему.
Остальные рекомендации с исходом встают в очередь.
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "create_triage_digest_outcomes",
        """CREATE TABLE IF NOT EXISTS triage_digest_outcomes (
            recommendation_id INTEGER PRIMARY KEY,
            digest_id         INTEGER,
            kind              TEXT    CHECK (kind IS NULL
                                             OR kind IN ('day', 'late')),
            created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "index_triage_digest_outcomes_pending",
        "CREATE INDEX IF NOT EXISTS idx_triage_digest_outcomes_pending "
        "ON triage_digest_outcomes(recommendation_id) WHERE digest_id IS NULL",
    ),
    (
        # Сутки создания читаются диапазоном: без индекса это полный проход.
        "index_executor_triage_recommendations_created",
        "CREATE INDEX IF NOT EXISTS idx_executor_triage_recommendations_created "
        "ON executor_triage_recommendations(created_at)",
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
    (
        "trg_triage_outcome_queued_on_insert",
        """CREATE TRIGGER IF NOT EXISTS trg_triage_outcome_queued_on_insert
        AFTER INSERT ON executor_triage_recommendations
        WHEN NEW.followup IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO triage_digest_outcomes (recommendation_id)
            VALUES (NEW.id);
        END""",
    ),
    (
        "trg_triage_outcome_queued_on_followup",
        """CREATE TRIGGER IF NOT EXISTS trg_triage_outcome_queued_on_followup
        AFTER UPDATE OF followup ON executor_triage_recommendations
        WHEN NEW.followup IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO triage_digest_outcomes (recommendation_id)
            VALUES (NEW.id);
        END""",
    ),
    (
        # Удалённая рекомендация (каскадом с задачей или заказом) уходит из очереди:
        # иначе удалённое копилось бы в частичном индексе и росло вместе с историей.
        "trg_triage_outcome_dequeued_on_delete",
        """CREATE TRIGGER IF NOT EXISTS trg_triage_outcome_dequeued_on_delete
        AFTER DELETE ON executor_triage_recommendations
        BEGIN
            DELETE FROM triage_digest_outcomes
            WHERE recommendation_id = OLD.id AND digest_id IS NULL;
        END""",
    ),
    (
        "queue_triage_outcomes_without_a_link",
        "INSERT OR IGNORE INTO triage_digest_outcomes (recommendation_id) "
        "SELECT id FROM executor_triage_recommendations WHERE followup IS NOT NULL",
    ),
]
