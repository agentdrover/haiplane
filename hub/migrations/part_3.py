"""Миграции схемы, часть 3 из 3 (#1809). Новые миграции — в конец этого списка."""

from __future__ import annotations

MIGRATIONS_3: list[tuple[str, str]] = [
    (
        # #1254: символы only_tests, названные ревьюеру в ЭТОМ заказе, JSON-
        # списком. Бриф судит итог ровно по нему, а не по второму разбору,
        # который мог увидеть другое дерево или протухшую базу. NULL — разбор
        # при заказе не состоялся (или строка старше колонки), «[]» — прошёл
        # и не назвал никого: разные ответы (#750).
        "add_review_dispatches_only_tests",
        "ALTER TABLE review_dispatches ADD COLUMN only_tests TEXT",
    ),
    # --- #1343: реестр мержей гейта держится за мерж, а не за номер PR. ------
    #
    # UNIQUE (project_id, pr_number) плюс INSERT OR IGNORE молча выбрасывали
    # новый мерж, если его номер PR уже занят строкой старого репозитория:
    # проект переехал, нумерация PR началась заново, 224 строки до PR 471
    # остались. С 15.09 так потеряны 31 доставка, и drift-guard называл мержи
    # гейта «мимо гейта». Номер PR — текст для контекста (#534), доказательство
    # мержа — merge_sha, им и ключуемся.
    #
    # В SQLite ограничение таблицы снимается только пересборкой. Шаги идут
    # отдельными записями, потому что _migrate исполняет по одному оператору,
    # но транзакция у них одна: _migrate коммитит один раз в конце, а первая
    # же отметка в _migrations открывает транзакцию. Упадёт любой шаг — откат
    # вернёт старую таблицу со строками, и ни одна отметка rekey_* не
    # сохранится (test_pipeline_merges_rekey_is_all_or_nothing). Первым шагом
    # идёт уборка копии, чтобы повтор после сбоя начинал с чистого места.
    (
        "rekey_pipeline_merges_clear",
        "DROP TABLE IF EXISTS pipeline_merges_rekeyed",
    ),
    (
        "rekey_pipeline_merges_create",
        """CREATE TABLE pipeline_merges_rekeyed (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER,
            pr_number    INTEGER NOT NULL,
            task_id      INTEGER,
            merged_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            merge_sha    TEXT,
            released_pr  INTEGER,
            released_sha TEXT
        )""",
    ),
    (
        # id переносится явно: на него ссылаются ORDER BY id в читателях, и
        # строки остаются теми же строками, а не новыми с теми же данными.
        "rekey_pipeline_merges_copy",
        "INSERT INTO pipeline_merges_rekeyed (id, project_id, pr_number, "
        "task_id, merged_at, merge_sha, released_pr, released_sha) "
        "SELECT id, project_id, pr_number, task_id, merged_at, merge_sha, "
        "released_pr, released_sha FROM pipeline_merges",
    ),
    (
        # AUTOINCREMENT обещает не выдавать id повторно, в том числе id
        # удалённых строк. Копия знает только максимум уцелевших, поэтому
        # счётчик старой таблицы переносится, пока она ещё есть.
        "rekey_pipeline_merges_sequence",
        "UPDATE sqlite_sequence SET seq = MAX(seq, COALESCE((SELECT s.seq "
        "FROM sqlite_sequence s WHERE s.name = 'pipeline_merges'), 0)) "
        "WHERE name = 'pipeline_merges_rekeyed'",
    ),
    (
        "rekey_pipeline_merges_drop",
        "DROP TABLE pipeline_merges",
    ),
    (
        "rekey_pipeline_merges_rename",
        "ALTER TABLE pipeline_merges_rekeyed RENAME TO pipeline_merges",
    ),
    (
        # Ключ — мерж. Индекс частичный: пустой merge_sha значит «коммит не
        # прочитан» (писатели передают '' при сбое чтения), и это не ключ.
        # Без WHERE все такие строки проекта схлопнулись бы в одну, то есть
        # пустой sha глотал бы чужие мержи — та же болезнь, что лечится.
        # Повтор мержа с пустым sha гасит record_pipeline_merge.
        "rekey_pipeline_merges_unique_sha",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_pipeline_merges_project_sha "
        "ON pipeline_merges (project_id, merge_sha) WHERE merge_sha <> ''",
    ),
    (
        # Прежний UNIQUE служил и индексом по проекту; поиск остаётся, а
        # уникальности больше нет.
        "rekey_pipeline_merges_project_pr_index",
        "CREATE INDEX IF NOT EXISTS ix_pipeline_merges_project_pr "
        "ON pipeline_merges (project_id, pr_number)",
    ),
    (
        # #1399: бронь облачного заказа ревью на (сдача, профиль). Ключ —
        # первичный, и INSERT OR IGNORE по нему решает, кто из параллельных
        # триггеров покупает ревьюера. Отдельной таблицей, а не уникальным
        # индексом на review_dispatches: строка заказа пишется ПОСЛЕ
        # оплаченного вызова провайдера, и индекс остановил бы второго уже
        # купившим агента. Заодно миграции не нужно переживать дубли, уже
        # лежащие в review_dispatches (412/413, 347/348): их она не трогает.
        "create_review_order_claims",
        """CREATE TABLE IF NOT EXISTS review_order_claims (
            task_id               INTEGER NOT NULL
                                  REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL,
            profile               TEXT    NOT NULL,
            claimed_at            TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, submission_generation, profile)
        )""",
    ),
    (
        # #1361: отчёт, ПЕРЕНЕСЁННЫЙ на пересдачу «только слита база», а не
        # прочитанный заново. Непустое значение — id исходного отчёта: по нему
        # перенос помечен в карточке и исключён из метрик чтения. NULL — всё,
        # что было до этого, и каждое настоящее чтение после.
        "add_machine_reviews_carried_from_review_id",
        "ALTER TABLE machine_reviews ADD COLUMN carried_from_review_id INTEGER",
    ),
    (
        # #1410 (F2.2): строка прогона облачного исполнителя. В F0 прогон
        # висел 12 часов, а цену узнали вручную — хаб её не писал. tokens и
        # cents (chargedCents провайдера) NULL, пока провайдер их не назвал:
        # неизвестно — не ноль. outcome: running | finished | cancelled |
        # failed | over_ceiling | taken_down; reason — почему строка стоит
        # как стоит (например, провайдер не ответил на опрос).
        "create_executor_runs",
        """CREATE TABLE IF NOT EXISTS executor_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               INTEGER NOT NULL
                                  REFERENCES tasks(id) ON DELETE CASCADE,
            submission_generation INTEGER NOT NULL DEFAULT 0,
            agent_id              TEXT    NOT NULL DEFAULT '',
            run_id                TEXT    NOT NULL DEFAULT '',
            model                 TEXT    NOT NULL DEFAULT '',
            tokens                INTEGER,
            cents                 REAL,
            started_at            TEXT    NOT NULL DEFAULT (datetime('now')),
            finished_at           TEXT,
            duration_ms           INTEGER,
            outcome               TEXT    NOT NULL DEFAULT 'running',
            reason                TEXT    NOT NULL DEFAULT '',
            polled_at             TEXT
        )""",
    ),
    (
        "idx_executor_runs_outcome",
        "CREATE INDEX IF NOT EXISTS idx_executor_runs_outcome "
        "ON executor_runs(outcome, task_id)",
    ),
    (
        # #1410: когда опрос впервые увидел конец прогона без цены. От этой
        # отметки считается EXECUTOR_COST_WAIT_MIN; NULL — цены не ждём.
        "add_executor_runs_cost_wait_since",
        "ALTER TABLE executor_runs ADD COLUMN cost_wait_since TEXT",
    ),
    (
        # #1411 (F2.3): потолки прогона, заданные при заказе, — хаб держит их
        # сам, не промпт исполнителя. NULL у строк до миграции читается как
        # умолчание из конфигурации.
        "add_executor_runs_token_ceiling",
        "ALTER TABLE executor_runs ADD COLUMN token_ceiling INTEGER",
    ),
    (
        "add_executor_runs_cents_ceiling",
        "ALTER TABLE executor_runs ADD COLUMN cents_ceiling REAL",
    ),
    (
        # #1411: зачем хаб отменяет прогон (taken_down | over_ceiling) — этим
        # исходом строка закроется, когда прогон прочитается CANCELLED.
        # Пусто — хаб отмену не начинал.
        "add_executor_runs_cancel_intent",
        "ALTER TABLE executor_runs ADD COLUMN cancel_intent TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1411: попытки отмены и время последней — счёт до потолка попыток и
        # пауза между ними переживают проход поллера и перезапуск.
        "add_executor_runs_cancel_attempts",
        "ALTER TABLE executor_runs "
        "ADD COLUMN cancel_attempts INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "add_executor_runs_cancel_last_at",
        "ALTER TABLE executor_runs ADD COLUMN cancel_last_at TEXT",
    ),
    (
        # #1563: ссылка, от которой хаб заказал прогон (startingRef провайдера).
        # NULL — у строк до миграции и у заказа, не дошедшего до записи: «не
        # записано», а не «база».
        "add_executor_runs_starting_ref",
        "ALTER TABLE executor_runs ADD COLUMN starting_ref TEXT",
    ),
    (
        # #1683: версия навыка дисциплины, вставленная в заказ. NULL — у строк
        # до миграции и у заказа, не дошедшего до записи: «не записано», и
        # читатель отдаёт навык целиком.
        "add_executor_runs_ordered_skill_version",
        "ALTER TABLE executor_runs ADD COLUMN ordered_skill_version INTEGER",
    ),
    (
        # #1683: какой заказ выписал код implementer; сессия наследует связь
        # при обмене. NULL — ручной код и строки до миграции.
        "add_chat_pair_codes_executor_run_id",
        "ALTER TABLE chat_pair_codes ADD COLUMN executor_run_id INTEGER",
    ),
    (
        "add_chat_pair_sessions_executor_run_id",
        "ALTER TABLE chat_pair_sessions ADD COLUMN executor_run_id INTEGER",
    ),
    # #1783: метка захвата сессии (claim_nonce), прогон-держатель (executor_run_id)
    (
        "add_chat_pair_sess_claim",
        "ALTER TABLE chat_pair_sessions ADD COLUMN claimed_session TEXT",
    ),
    ("t1783_task_nonce", "ALTER TABLE tasks ADD COLUMN claim_nonce TEXT"),
    ("t1783_task_run", "ALTER TABLE tasks ADD COLUMN executor_run_id INTEGER"),
    (
        # #1783: захват снят — доказательство уходит с ним на любом пути.
        "trg_tasks_claim_cleared_drops_run_proof",
        "CREATE TRIGGER IF NOT EXISTS trg_tasks_claim_cleared_drops_run_proof AFTER "
        "UPDATE OF claimed_by ON tasks WHEN NEW.claimed_by IS NULL AND "
        "NEW.executor_run_id IS NOT NULL BEGIN UPDATE tasks SET executor_run_id = NULL "
        "WHERE id = NEW.id; END",
    ),
    (
        # #1420: открытые аварии релиза читаются каждым общим hub_my_context —
        # последнее событие release_blocked/unblocked по проекту без скана ленты.
        "idx_events_kind_project",
        "CREATE INDEX IF NOT EXISTS idx_events_kind_project "
        "ON events(kind, project_id, id)",
    ),
    (
        # #1645: heartbeat of the egress watcher — ONE row, written in the same
        # transaction as an episode transition. checked_at lives here, not in
        # memory: a restart keeps it, every process sees the same one, and a
        # failed write leaves it stale (-> unknown) instead of a false "up".
        "create_egress_state_table",
        "CREATE TABLE IF NOT EXISTS egress_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "checked_at TEXT NOT NULL, "
        "interval_seconds INTEGER NOT NULL, "
        "ok INTEGER NOT NULL, "
        "reason_code TEXT NOT NULL DEFAULT '', "
        "streak INTEGER NOT NULL DEFAULT 0, "
        "first_fail_at TEXT NOT NULL DEFAULT '')",
    ),
    (
        # #1413: чем снят provider_tokens. '' — история до задачи (счёт
        # ОДНОГО прогона, переснимается только командой владельца);
        # agent_open — счёт агента, снятый до его конца, свип переснимет;
        # agent — итог по агенту; recounted — пересчитан командой владельца.
        "add_review_dispatches_usage_scope",
        "ALTER TABLE review_dispatches ADD COLUMN usage_scope TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1413: когда провайдера спрашивали о счёте этого заказа последний
        # раз — переснятие ходит не чаще раза в USAGE_RESTAMP_EVERY_MINUTES.
        "add_review_dispatches_usage_checked_at",
        "ALTER TABLE review_dispatches ADD COLUMN usage_checked_at TEXT",
    ),
    (
        # #1403: кто назначил профиль заказа — правило (rule) или жребий над
        # lite правила риска (random). Старые строки — rule.
        "add_review_dispatches_profile_assignment",
        "ALTER TABLE review_dispatches "
        "ADD COLUMN profile_assignment TEXT NOT NULL DEFAULT 'rule'",
    ),
    (
        # #1413: полная цена заказа по выгрузке Cursor, которую приносит
        # владелец. Рядом с provider_tokens (API агента — нижняя граница),
        # не вместо него. NULL — выгрузка по агенту не импортировалась.
        "add_review_dispatches_billed_tokens",
        "ALTER TABLE review_dispatches ADD COLUMN billed_tokens INTEGER",
    ),
    (
        "add_review_dispatches_billed_events",
        "ALTER TABLE review_dispatches ADD COLUMN billed_events INTEGER",
    ),
    (
        # #1413: события выгрузки Cursor, по одному на ключ (агент, время,
        # токены, модель) — повторный и перекрывающийся импорт не удваивает.
        # Хранятся только события заказов ревью хаба.
        "create_cursor_usage_events",
        """CREATE TABLE IF NOT EXISTS cursor_usage_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id     TEXT    NOT NULL,
            event_date   TEXT    NOT NULL,
            total_tokens INTEGER NOT NULL,
            model        TEXT    NOT NULL DEFAULT '',
            dispatch_id  INTEGER,
            imported_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (agent_id, event_date, total_tokens, model)
        )""",
    ),
    # Строки #1413 стоят до блока #1434 намеренно: его тест требует свои
    # миграции последними, а применение идёт по имени — порядок не важен.
    # #917 стоит здесь по той же причине.
    (
        # #1590: итог проверенного бэкапа базы перед рестартом (строка из
        # remote-deploy.sh). Пусто — «неизвестно»: отчёт старого клиента.
        "add_releases_backup",
        "ALTER TABLE releases ADD COLUMN backup TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #917 (F5): релиз, в котором проявился дефект. Ссылка на строку
        # releases, а не sha: «что работало в проде» уже записано там (#495),
        # и второй копии факта расходиться не с чем. NULL — «не указан», а не
        # «ни в каком»; #915 заполняет её при заведении прод-дефекта.
        "add_tasks_release_id",
        "ALTER TABLE tasks ADD COLUMN release_id INTEGER REFERENCES releases(id)",
    ),
    (
        "idx_tasks_release_id",
        "CREATE INDEX IF NOT EXISTS idx_tasks_release_id ON tasks(release_id)",
    ),
    (
        # #917: предложенный машиной виновник — ГИПОТЕЗА, отдельно от
        # tasks.caused_by_task_id, который ставит только явное подтверждение.
        # Строка пишется один раз на тройку (дефект, кандидат, релиз) и не
        # удаляется: доля подтверждённых предложений (outcome #917) считается
        # по тому, что было предложено, а не по тому, что предлагается сейчас.
        "create_defect_cause_suggestions",
        """CREATE TABLE IF NOT EXISTS defect_cause_suggestions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            candidate_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            release_id        INTEGER NOT NULL REFERENCES releases(id),
            overlap           TEXT    NOT NULL DEFAULT '[]',
            suggested_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (defect_task_id, candidate_task_id, release_id)
        )""",
    ),
    (
        # #1434 (F7): канал (cloud | slot) и имя слота — атрибуты выдачи кода
        # implementer. Пусто — канал не назван: код без канала работает как
        # до задачи (облачный исполнитель, пилот SID).
        "add_chat_pair_codes_channel",
        "ALTER TABLE chat_pair_codes ADD COLUMN channel TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_chat_pair_codes_slot",
        "ALTER TABLE chat_pair_codes ADD COLUMN slot TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_chat_pair_sessions_channel",
        "ALTER TABLE chat_pair_sessions ADD COLUMN channel TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_chat_pair_sessions_slot",
        "ALTER TABLE chat_pair_sessions ADD COLUMN slot TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1434: захват задачи в названном канале. Строка живёт от захвата до
        # released_at: outcome occupied -> freed (задача ушла из работы: сдача,
        # возврат, отказ) или released_dead (поллер освободил слот без
        # признаков жизни, reason называет, сколько минут тишины).
        "create_executor_slots",
        """CREATE TABLE IF NOT EXISTS executor_slots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            channel      TEXT    NOT NULL,
            slot         TEXT    NOT NULL DEFAULT '',
            captured_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            released_at  TEXT,
            outcome      TEXT    NOT NULL DEFAULT 'occupied',
            reason       TEXT    NOT NULL DEFAULT ''
        )""",
    ),
    (
        # Одна живая строка на задачу: повторный захват (claim, затем
        # pair-start той же сессией) — INSERT OR IGNORE, а не вторая строка.
        "idx_executor_slots_active",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_slots_active "
        "ON executor_slots(task_id) WHERE released_at IS NULL",
    ),
    ("t1783_slot_run", "ALTER TABLE executor_slots ADD COLUMN executor_run_id INTEGER"),
    (
        # #919: what a closed production defect left behind — a regression
        # test, a rule from category_checks (#878), or an accepted risk with a
        # reason and a revisit condition. One JSON column next to the passport
        # (#909) rather than a table: there is one answer per defect, and it
        # travels with the task it answers. NULL = no answer recorded; old rows
        # stay NULL — back-filling "accepted" would invent the very output the
        # gate exists to require.
        "add_defect_prevention_column",
        "ALTER TABLE tasks ADD COLUMN defect_prevention TEXT",
    ),
    (
        # #920: when a rule was SET UP, apart from when its current check was
        # recorded. ``recorded_at`` moves on every re-record — a better check
        # replaces the claim (#878) — so a repeat report reading it would
        # forgive every finding between the first check and the better one,
        # and a rule could be made to look unbroken by re-recording it. The
        # breach is counted from this date; the upsert never touches it.
        "add_category_checks_created_at",
        "ALTER TABLE category_checks ADD COLUMN created_at TEXT",
    ),
    (
        # Old rows: the earliest ``category_check_recorded`` event for the
        # category (#878 writes one on every record), else ``recorded_at``.
        # Never later than ``recorded_at``: a rule cannot be younger than its
        # current check. Only NULLs are filled, so a re-run changes nothing.
        "backfill_category_checks_created_at",
        "UPDATE category_checks SET created_at = MIN(recorded_at, COALESCE(("
        "SELECT MIN(e.created_at) FROM events e "
        "WHERE e.kind = 'category_check_recorded' AND CASE WHEN "
        "json_valid(e.payload) THEN json_extract(e.payload, '$.category') END "
        "= category_checks.category), recorded_at)) WHERE created_at IS NULL",
    ),
    (
        # Red-test baseline (#913): the branch's changed test files run over
        # the merge-base code, a status per nodeid. Its own column next to
        # ``mutations``: the red-test gate reads it at submission, and '{}' for
        # every older report means "no baseline was reported" — the gate says
        # "not proven", never "proven".
        "add_ci_run_reports_baseline",
        "ALTER TABLE ci_run_reports ADD COLUMN baseline TEXT NOT NULL DEFAULT '{}'",
    ),
    (
        # #1589: what the project's path_notices rules said about the paths of
        # ONE submission generation. A table, not an event: events are pruned
        # after 14 days and a verdict can wait longer, and ``submissions`` is
        # written only by the pair path. UNIQUE(task_id, generation): the
        # result is fixed at submission and never rewritten, so a policy edit
        # afterwards cannot change what the reviewer is shown.
        "create_path_notice_results",
        """CREATE TABLE IF NOT EXISTS path_notice_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            sha        TEXT    NOT NULL DEFAULT '',
            state      TEXT    NOT NULL,
            reason     TEXT    NOT NULL DEFAULT '',
            notices    TEXT    NOT NULL DEFAULT '[]',
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, generation)
        )""",
    ),
    # --- #1601: советник-критик стюарда и критерий выхода из тени v2. -------
    (
        # Для kind=advisor: id суждения судьи, на которое отвечает советник.
        # Привязку ставит хаб по заказу, а не советник словами.
        "add_steward_judgements_judged_id",
        "ALTER TABLE steward_judgements ADD COLUMN judged_id INTEGER",
    ),
    (
        # Хеш пакета фактов (steward_evidence.packet_hash), который прочёл
        # прогон: хаб снимает его при выдаче пакета и переносит в суждение.
        # Пусто — пакета под этот прогон не отдавали, и согласие не засчитывается.
        "add_steward_judgements_packet_hash",
        "ALTER TABLE steward_judgements ADD COLUMN packet_hash TEXT "
        "NOT NULL DEFAULT ''",
    ),
    (
        # Контур выборки: 1 — суждения до выката #1601 (одиночные, без
        # советника), 2 — новый контур. Старые строки остаются 1 по умолчанию
        # колонки; критерий выхода v2 их не зачитывает.
        "add_steward_judgements_contour",
        "ALTER TABLE steward_judgements ADD COLUMN contour INTEGER NOT NULL DEFAULT 1",
    ),
    (
        # Исход применения approve судьи (поллер): '' — ещё не применялся;
        # approved | escalated | moot. Ставится условным UPDATE: так применение
        # случается ровно один раз и переживает перезапуск хаба.
        "add_steward_judgements_advisor_outcome",
        "ALTER TABLE steward_judgements ADD COLUMN advisor_outcome TEXT "
        "NOT NULL DEFAULT ''",
    ),
    (
        # Когда применение заняло метку (advisor_outcome='applying'): по ней
        # прервавшееся применение отличают от идущего (#1601).
        "add_steward_judgements_advisor_claimed_at",
        "ALTER TABLE steward_judgements ADD COLUMN advisor_claimed_at TEXT",
    ),
    (
        "add_steward_runs_packet_hash",
        "ALTER TABLE steward_runs ADD COLUMN packet_hash TEXT NOT NULL DEFAULT ''",
    ),
    (
        # Липкое ошибочное одобрение пары (#1601). Таблица, а не событие:
        # события чистятся через 14 дней, а окно проверки — 30 дней после
        # доставки. Строка живёт, пока человек явно не снимет её (cleared_*).
        # Ключ — КОНКРЕТНЫЙ случай (задача, источник, ref): снятие закрывает
        # случай, а не задачу, новый случай — новая строка.
        "create_steward_false_approvals",
        """CREATE TABLE IF NOT EXISTS steward_false_approvals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     INTEGER NOT NULL,
            source      TEXT    NOT NULL,
            generation  INTEGER NOT NULL DEFAULT 0,
            detail      TEXT    NOT NULL DEFAULT '',
            ref         TEXT    NOT NULL DEFAULT '',
            detected_at TEXT    NOT NULL DEFAULT (datetime('now')),
            cleared_by  TEXT,
            cleared_at  TEXT,
            clear_note  TEXT    NOT NULL DEFAULT '',
            UNIQUE (task_id, source, ref)
        )""",
    ),
    # --- #1601: факт ошибочного одобрения пишется В МОМЕНТ события, в той же
    # транзакции, что и оно. Триггеры БД, а не вызовы в путях: путей записи
    # вердикта и выхода из completed много, триггер ловит и забытые.
    (
        # Сколько раз задача выходила из completed (#1601): порядковый номер
        # случая «переоткрытие», не зависящий от часов.
        "add_tasks_completed_exit_seq",
        "ALTER TABLE tasks ADD COLUMN completed_exit_seq INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # Человеческий возврат. Список акторов автоматики — как
        # gate_events.NON_HUMAN_GATE_ACTORS; расхождение ловит тест (новый
        # актор = новая миграция, пересоздающая триггер).
        "create_trigger_false_approve_human_return",
        """CREATE TRIGGER IF NOT EXISTS trg_false_approve_human_return
        AFTER INSERT ON events
        WHEN NEW.kind = 'review_verdict_recorded' AND NEW.task_id IS NOT NULL
          AND NEW.actor NOT IN ('hub', 'policy', 'steward')
          AND json_extract(NEW.payload, '$.verdict') = 'changes_requested'
        BEGIN
            INSERT OR IGNORE INTO steward_false_approvals
              (task_id, source, generation, detail, ref)
            SELECT j.task_id, 'human_changes_requested', j.generation, 'человек вернул поколение ' || COALESCE(json_extract(NEW.payload, '$.submission_generation'), 0) || ' (пара одобрила поколение ' || j.generation || '), событие #' || NEW.id, CAST(NEW.id AS TEXT)
            FROM steward_judgements a
            JOIN steward_judgements j ON j.id = a.judged_id
            WHERE a.kind = 'advisor' AND a.verdict = 'concur'
              AND j.kind = 'verdict' AND j.verdict = 'approve' AND j.contour = 2
              AND a.task_id = NEW.task_id AND j.generation <= COALESCE(json_extract(NEW.payload, '$.submission_generation'), 0);
        END""",
    ),
    (
        # Выход из completed любым путём (transition_status_if, update_task,
        # прямой UPDATE). Ref — порядковый номер выхода из completed у этой
        # задачи (счётчик completed_exit_seq, растёт в самом триггере), а не
        # время: два выхода в одну миллисекунду с одинаковым completed_at —
        # два разных случая. Последующие переходы нового случая не создают.
        "create_trigger_false_approve_reopened",
        """CREATE TRIGGER IF NOT EXISTS trg_false_approve_reopened
        AFTER UPDATE OF status ON tasks
        WHEN OLD.status = 'completed' AND NEW.status != 'completed'
        BEGIN
            UPDATE tasks SET completed_exit_seq = completed_exit_seq + 1
            WHERE id = NEW.id;
            INSERT OR IGNORE INTO steward_false_approvals
              (task_id, source, generation, detail, ref)
            SELECT j.task_id, 'reopened', j.generation, 'задача вышла из completed в ' || NEW.status, 'exit:' || (SELECT completed_exit_seq FROM tasks WHERE id = NEW.id)
            FROM steward_judgements a
            JOIN steward_judgements j ON j.id = a.judged_id
            WHERE a.kind = 'advisor' AND a.verdict = 'concur'
              AND j.kind = 'verdict' AND j.verdict = 'approve' AND j.contour = 2
              AND a.task_id = NEW.id;
        END""",
    ),
    (
        "create_trigger_false_approve_prod_defect_insert",
        """CREATE TRIGGER IF NOT EXISTS trg_false_approve_prod_defect_insert
        AFTER INSERT ON tasks
        WHEN NEW.found_in = 'prod' AND NEW.caused_by_task_id IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO steward_false_approvals
              (task_id, source, generation, detail, ref)
            SELECT j.task_id, 'prod_defect', j.generation, 'прод-дефект found_in=prod, caused_by_task_id на неё: #' || NEW.id, CAST(NEW.id AS TEXT)
            FROM steward_judgements a
            JOIN steward_judgements j ON j.id = a.judged_id
            WHERE a.kind = 'advisor' AND a.verdict = 'concur'
              AND j.kind = 'verdict' AND j.verdict = 'approve' AND j.contour = 2
              AND a.task_id = NEW.caused_by_task_id;
        END""",
    ),
    (
        "create_trigger_false_approve_prod_defect_update",
        """CREATE TRIGGER IF NOT EXISTS trg_false_approve_prod_defect_update
        AFTER UPDATE OF found_in, caused_by_task_id ON tasks
        WHEN NEW.found_in = 'prod' AND NEW.caused_by_task_id IS NOT NULL
          AND (OLD.found_in IS NOT 'prod'
               OR OLD.caused_by_task_id IS NOT NEW.caused_by_task_id)
        BEGIN
            INSERT OR IGNORE INTO steward_false_approvals
              (task_id, source, generation, detail, ref)
            SELECT j.task_id, 'prod_defect', j.generation, 'прод-дефект found_in=prod, caused_by_task_id на неё: #' || NEW.id, CAST(NEW.id AS TEXT)
            FROM steward_judgements a
            JOIN steward_judgements j ON j.id = a.judged_id
            WHERE a.kind = 'advisor' AND a.verdict = 'concur'
              AND j.kind = 'verdict' AND j.verdict = 'approve' AND j.contour = 2
              AND a.task_id = NEW.caused_by_task_id;
        END""",
    ),
    (
        # #1593: отложенные правки политики проекта. Своя таблица, не ключ
        # gate_policy: политика остаётся тем, что хаб ЧИТАЕТ, а расписание —
        # тем, что хаб ИСПОЛНЯЕТ; ключ расписания внутри политики прошёл бы
        # валидатор и замок как «политика», а исполнитель должен читать его
        # другим кодом. ``at`` — UTC в одном формате (``%Y-%m-%dT%H:%M:%SZ``),
        # поэтому порядок (at, id) — порядок строк. State меняет хаб
        # (pending → applied|refused) или человек (pending → cancelled);
        # исполненная запись больше не редактируется.
        "create_scheduled_policy_changes",
        """CREATE TABLE IF NOT EXISTS scheduled_policy_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id),
            at          TEXT    NOT NULL,
            patch       TEXT    NOT NULL,
            note        TEXT    NOT NULL DEFAULT '',
            created_by  TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            state       TEXT    NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'applied', 'refused', 'cancelled')),
            executed_at TEXT,
            result      TEXT    NOT NULL DEFAULT '{}'
        )""",
    ),
    (
        "idx_scheduled_policy_changes_due",
        "CREATE INDEX IF NOT EXISTS idx_scheduled_policy_changes_due "
        "ON scheduled_policy_changes(state, at, id)",
    ),
    (
        # #1594: обоснование допуска работы при заморозке проекта. Отдельно от
        # ``rationale`` (общая необходимость задачи): пустое после strip значит
        # «обоснования нет», и заморозка такую работу не пускает.
        "add_tasks_freeze_rationale",
        "ALTER TABLE tasks ADD COLUMN freeze_rationale TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1647: что является результатом работы. ``commit`` — у всех
        # существующих задач (DEFAULT), ``state`` — состояние мира без ветки,
        # PR и CI. Меняется только в draft: проверка в refinement под
        # write-транзакцией, не в схеме.
        "add_tasks_result_kind",
        "ALTER TABLE tasks ADD COLUMN result_kind TEXT NOT NULL DEFAULT 'commit'",
    ),
    (
        # #1647: способ отката результата-состояния. Пусто у commit-задач.
        "add_tasks_rollback",
        "ALTER TABLE tasks ADD COLUMN rollback TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1647: поколение, на котором человек явно вернул state-задачу в работу
        # (return-to-work, rework). Постановка state-задачи заморожена, пока
        # поколение сдачи не равно этому числу; новая сдача поднимает поколение
        # и замораживает её снова. Признак возврата, а не статуса: вопрос
        # агента (needs_info) заморозку не снимает.
        "add_tasks_unfrozen_generation",
        "ALTER TABLE tasks ADD COLUMN unfrozen_generation INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # #1647: снимок AC, rollback и result_kind на момент сдачи state-задачи
        # (JSON). Доказательства относятся к снимку, а не к тому, что AC
        # скажут потом. Пусто у сдач commit-задач.
        "add_submissions_state_snapshot",
        "ALTER TABLE submissions ADD COLUMN state_snapshot TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1674: проекты, которые сессия объявила при регистрации (JSON-массив
        # slug). '' у существующих строк читается как «не объявлено».
        "add_agent_sessions_projects",
        "ALTER TABLE agent_sessions ADD COLUMN projects TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1678: рабочая роль, которую сессия объявила при регистрации
        # (steward|executor|watcher|reviewer). '' у существующих строк — «не
        # объявлена». Декларация для адресата to_kind='role', не право.
        "add_agent_sessions_work_role",
        "ALTER TABLE agent_sessions ADD COLUMN work_role TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1679: пожизненный дедуп уведомлений стюарду; ключи не стареют.
        "create_watch_notices",
        "CREATE TABLE IF NOT EXISTS watch_notices (key TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))",
    ),
    (
        # #1647: доказательства сдачи state-задачи. Одна строка на (задача,
        # поколение, AC). Только вставка: UPDATE запрещён триггером ниже, а
        # кода, который правит или удаляет строки, нет. principal_id — автор из
        # идентичности вызывающего; agent — подпись, которую назвал клиент.
        # Каскад по задаче оставлен: удаление самой задачи уносит и её след.
        "create_task_evidence",
        """CREATE TABLE IF NOT EXISTS task_evidence (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation   INTEGER NOT NULL,
            ac_id        TEXT    NOT NULL,
            action       TEXT    NOT NULL,
            observed     TEXT    NOT NULL,
            target       TEXT    NOT NULL,
            observed_at  TEXT    NOT NULL,
            principal_id INTEGER,
            agent        TEXT    NOT NULL DEFAULT '',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, generation, ac_id)
        )""",
    ),
    (
        "idx_task_evidence_task",
        "CREATE INDEX IF NOT EXISTS idx_task_evidence_task "
        "ON task_evidence(task_id, generation)",
    ),
    (
        "task_evidence_no_update",
        """CREATE TRIGGER IF NOT EXISTS task_evidence_no_update
        BEFORE UPDATE ON task_evidence
        BEGIN
            SELECT RAISE(ABORT, 'task_evidence is insert-only');
        END""",
    ),
    (
        # #1686: which GitHub run produced a CI report — a JSON object (run_id,
        # run_attempt, workflow_path, event, head_repository, run_conclusion,
        # artifact_id, artifact_digest). '{}' = the reporter did not say (every
        # report before this task, and any reporter that predates the trusted
        # workflow). Recorded, not checked: the check is the hub's next step.
        "add_ci_run_reports_provenance",
        "ALTER TABLE ci_run_reports ADD COLUMN provenance TEXT NOT NULL DEFAULT '{}'",
    ),
    # ---- #1687: the hub no longer takes a CI report on its word. ci_run_reports
    # keeps ONE row per (task, commit) that every reader already uses, and from
    # now on it is the projection of the CURRENT attempt (ci_report_attempts
    # holds every attempt separately). trust says whether GitHub confirmed the
    # report; the DEFAULT is the migration: a row written before this task has
    # no run to check, so it reads as unverified(legacy) and is never a pass.
    (
        "add_ci_run_reports_trust",
        "ALTER TABLE ci_run_reports ADD COLUMN trust TEXT NOT NULL DEFAULT 'unverified'",
    ),
    (
        "add_ci_run_reports_trust_reason",
        "ALTER TABLE ci_run_reports ADD COLUMN trust_reason TEXT NOT NULL DEFAULT 'legacy'",
    ),
    (
        "add_ci_run_reports_attempt_key",
        "ALTER TABLE ci_run_reports ADD COLUMN attempt_key TEXT NOT NULL DEFAULT ''",
    ),
    (
        # One row per (task, commit, run, attempt): a late POST of an old
        # attempt can no longer overwrite a newer one. ``submitted`` is the
        # whole comparable body as the reporter sent it (before the hub kept
        # only the AC it knows), which is what the artifact is compared with.
        # ``trust_final`` = 1 ends the retries (a definite answer, or the six
        # hours for an unreachable GitHub ran out). ``body_version`` counts the
        # replacements of the body: a verdict is written only for the version
        # it was computed for, so a body replaced mid-check is checked again.
        "create_ci_report_attempts",
        """CREATE TABLE IF NOT EXISTS ci_report_attempts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id           INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            head_sha          TEXT    NOT NULL,
            run_id            TEXT    NOT NULL DEFAULT '',
            run_attempt       TEXT    NOT NULL DEFAULT '',
            ac_results        TEXT    NOT NULL DEFAULT '{}',
            checks            TEXT    NOT NULL DEFAULT '{}',
            validation_status TEXT    NOT NULL DEFAULT '',
            validation_log    TEXT    NOT NULL DEFAULT '',
            reason            TEXT    NOT NULL DEFAULT '',
            reported_by       TEXT    NOT NULL DEFAULT '',
            mutations         TEXT    NOT NULL DEFAULT '{}',
            baseline          TEXT    NOT NULL DEFAULT '{}',
            provenance        TEXT    NOT NULL DEFAULT '{}',
            submitted         TEXT    NOT NULL DEFAULT '{}',
            body_version      INTEGER NOT NULL DEFAULT 0,
            trust             TEXT    NOT NULL DEFAULT 'unverified',
            trust_reason      TEXT    NOT NULL DEFAULT 'pending',
            trust_final       INTEGER NOT NULL DEFAULT 0,
            checked_at        TEXT,
            reported_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, head_sha, run_id, run_attempt)
        )""",
    ),
    (
        # The state version of one commit's CI standing: bumped by every POST
        # of any attempt and at the start of every refresh. A refresh writes
        # its projection and the evidence only if the version is still the one
        # it captured, in one short write transaction, so a picture taken
        # before a newer fact arrived can never be written over it — in this
        # process or in another one.
        "create_ci_report_state",
        """CREATE TABLE IF NOT EXISTS ci_report_state (
            task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            head_sha TEXT    NOT NULL,
            version  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, head_sha)
        )""",
    ),
    (
        # Reports stored before this task that DO name their run and artifact
        # (#1686) are not lost to "legacy": each becomes an unverified attempt,
        # queued for the check like any other (the poller picks up an attempt
        # that was never checked). ``partial`` marks that the AC map kept in the
        # row is already filtered to the AC the hub knew, so the comparison with
        # the artifact is limited to those. Reports without a named run stay
        # legacy. OR IGNORE: the migration may be applied to a database that
        # already holds attempts.
        "queue_provenanced_reports_for_verification",
        """INSERT OR IGNORE INTO ci_report_attempts (task_id, head_sha, run_id,
            run_attempt, ac_results, checks, validation_status, validation_log,
            reason, reported_by, mutations, baseline, provenance, submitted)
        SELECT task_id, head_sha, json_extract(provenance, '$.run_id'),
            json_extract(provenance, '$.run_attempt'), ac_results, checks,
            validation_status, validation_log, reason, reported_by, mutations,
            baseline, provenance,
            json_object('ac_results', json(ac_results), 'partial', 1)
        FROM ci_run_reports
        WHERE json_valid(provenance) AND json_valid(ac_results)
          AND COALESCE(json_extract(provenance, '$.run_id'), '') != ''
          AND COALESCE(json_extract(provenance, '$.run_attempt'), '') != ''
          AND COALESCE(json_extract(provenance, '$.artifact_id'), '') != ''
          AND COALESCE(json_extract(provenance, '$.artifact_digest'), '') != ''""",
    ),
    (
        # Where a stored AC result came from: 'manual' (the hub ran it),
        # 'ci:<run>:<attempt>' (written from a verified CI attempt) or 'legacy'.
        "add_ac_test_results_source",
        "ALTER TABLE ac_test_results ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
    ),
    (
        "add_tasks_validation_source",
        "ALTER TABLE tasks ADD COLUMN validation_source TEXT NOT NULL DEFAULT 'manual'",
    ),
    (
        # Existing CI evidence has no run to check: of the results already on a
        # task that ever got a CI report, none can be told from CI-written ones,
        # so they all become 'legacy' (unverified, not a pass). Idempotent: only
        # rows still marked 'manual' are touched.
        "legacy_ac_test_results_source",
        "UPDATE ac_test_results SET source='legacy' WHERE source='manual' "
        "AND task_id IN (SELECT task_id FROM ci_run_reports)",
    ),
    (
        "legacy_tasks_validation_source",
        "UPDATE tasks SET validation_source='legacy' WHERE validation_source='manual' "
        "AND validation_status IS NOT NULL "
        "AND id IN (SELECT task_id FROM ci_run_reports)",
    ),
    # ---- #1688: the diff of a submission is scanned for signs that it changes
    # how CI gets or passes its results (hub/services/ci_integrity.py).
    (
        # One row per (task, commit): the commit is immutable, so a scan of it
        # never goes stale and a resubmission with the same sha reuses it.
        # ``state`` is clean | signs | unreadable. A commit with NO row was
        # never scanned, which is its own state in the reader (unscanned) and
        # is never read as clean: absence of a scan is not absence of signs.
        "create_ci_integrity_scans",
        """CREATE TABLE IF NOT EXISTS ci_integrity_scans (
            task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            head_sha   TEXT    NOT NULL,
            state      TEXT    NOT NULL,
            signs      TEXT    NOT NULL DEFAULT '[]',
            reason     TEXT    NOT NULL DEFAULT '',
            scanned_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (task_id, head_sha)
        )""",
    ),
    (
        # The reviewer's answer to the integrity question, per report (reports
        # are per generation). '' = the reviewer did not answer: history, and
        # any report written without the field. Never back-filled with either
        # word — an empty answer is "did not answer", not "explained".
        "add_machine_reviews_ci_integrity_verdict",
        "ALTER TABLE machine_reviews ADD COLUMN ci_integrity_verdict TEXT "
        "NOT NULL DEFAULT ''",
    ),
    (
        "add_machine_reviews_ci_integrity_explanation",
        "ALTER TABLE machine_reviews ADD COLUMN ci_integrity_explanation TEXT "
        "NOT NULL DEFAULT ''",
    ),
    (
        # #1706: ревизия вердикта — сколько раз на задачу писали вердикт. Растёт
        # при КАЖДОЙ записи любым писателем (REST, web, стюард, автопилот,
        # поллер); замена стоящего вердикта сверяет её в условии UPDATE.
        # 0 — вердикта не было или он записан до колонки.
        "add_tasks_review_verdict_revision",
        "ALTER TABLE tasks ADD COLUMN review_verdict_revision INTEGER "
        "NOT NULL DEFAULT 0",
    ),
    (
        # #1706: отпечаток запроса, которым записан стоящий вердикт (вид,
        # находки, комментарий). По нему повтор того же запроса отличим от
        # другого: комментарий живёт только в ленте, а находки хранятся уже с
        # проставленными номерами follow-up задач. '' — вердикт записан
        # поллером или до колонки: повтор такого не узнаётся и получает 409.
        "add_tasks_review_verdict_digest",
        "ALTER TABLE tasks ADD COLUMN review_verdict_digest TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1700: how long one finished CI attempt of a task branch took. Statistics
        # only: no gate reads it, and it is written by the trusted workflow's key
        # from GitHub's own job records. One row per (task, run, attempt): a
        # resent timing updates the row, a rerun is a second row. ``completed_at``
        # is the completion time from the event ('YYYY-MM-DD HH:MM:SS' UTC, the
        # window of the metrics); ``state`` is ok | timing_unavailable. The
        # seconds are NULL when unknown — NULL is "not measured", never zero.
        "create_ci_attempt_timings",
        """CREATE TABLE IF NOT EXISTS ci_attempt_timings (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            run_id         TEXT    NOT NULL,
            run_attempt    TEXT    NOT NULL,
            head_sha       TEXT    NOT NULL DEFAULT '',
            event          TEXT    NOT NULL DEFAULT '',
            conclusion     TEXT    NOT NULL DEFAULT '',
            completed_at   TEXT    NOT NULL,
            state          TEXT    NOT NULL DEFAULT 'ok',
            reason         TEXT    NOT NULL DEFAULT '',
            jobs           TEXT    NOT NULL DEFAULT '[]',
            runner_seconds REAL,
            complete       INTEGER NOT NULL DEFAULT 0,
            steps_seconds  TEXT    NOT NULL DEFAULT '{}',
            reported_by    TEXT    NOT NULL DEFAULT '',
            received_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, run_id, run_attempt)
        )""",
    ),
    (
        # #1712: реестр хостов, которым хаб выдаёт код исполнителя сам. Один
        # хост — один сервисный принципал роли slot_runner и один открытый
        # ключ RSA (PEM SPKI), которым шифруется код. enabled=0 — хост
        # отключён человеком: его ключ отозван, коды сожжены.
        "create_executor_hosts",
        """CREATE TABLE IF NOT EXISTS executor_hosts (
            name           TEXT PRIMARY KEY,
            principal_id   INTEGER NOT NULL REFERENCES principals(id),
            public_key_pem TEXT    NOT NULL,
            enabled        INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            created_by     INTEGER REFERENCES principals(id),
            disabled_at    TEXT,
            disabled_by    INTEGER REFERENCES principals(id)
        )""",
    ),
    (
        # #1712: канал прогона, имя слота и кто нажал запуск. У строк до
        # миграции канал cloud — единственный, который существовал.
        "add_executor_runs_channel",
        "ALTER TABLE executor_runs ADD COLUMN channel TEXT NOT NULL DEFAULT 'cloud'",
    ),
    (
        "add_executor_runs_slot",
        "ALTER TABLE executor_runs ADD COLUMN slot TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_executor_runs_launched_by",
        "ALTER TABLE executor_runs ADD COLUMN launched_by INTEGER",
    ),
    (
        # #1712: заказ хосту = бронь слота от выпуска кода до закрытия.
        # state: pending -> delivered -> redeemed -> closed. Открытый код в
        # таблице не хранится: code_ciphertext — RSA-OAEP на ключ хоста, а
        # после закрытия очищается. pair_code_id — строка chat_pair_codes,
        # которую закрытие сжигает, пока код не потрачен.
        "create_executor_orders",
        """CREATE TABLE IF NOT EXISTS executor_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            host            TEXT    NOT NULL REFERENCES executor_hosts(name),
            task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            generation      INTEGER NOT NULL,
            executor_run_id INTEGER NOT NULL
                            REFERENCES executor_runs(id) ON DELETE CASCADE,
            pair_code_id    INTEGER,
            code_ciphertext TEXT    NOT NULL DEFAULT '',
            order_text      TEXT    NOT NULL DEFAULT '',
            state           TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (state IN
                                   ('pending', 'delivered', 'redeemed', 'closed')),
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at      TEXT    NOT NULL,
            delivered_at    TEXT,
            redeemed_at     TEXT,
            closed_at       TEXT,
            close_reason    TEXT    NOT NULL DEFAULT '',
            ack_result      TEXT    NOT NULL DEFAULT '',
            acked_at        TEXT,
            report_outcome  TEXT    NOT NULL DEFAULT '',
            report_sha      TEXT    NOT NULL DEFAULT '',
            report_branch   TEXT    NOT NULL DEFAULT '',
            report_note     TEXT    NOT NULL DEFAULT '',
            reported_at     TEXT
        )""",
    ),
    (
        # #1712: одна задача на слот ЗДЕСЬ, а не в проверке перед вставкой —
        # два нажатия с двух соединений не пройдут оба.
        "idx_executor_orders_one_open_per_host",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_executor_orders_one_open_per_host "
        "ON executor_orders(host) WHERE state <> 'closed'",
    ),
    (
        # #1712: хеш кода заказа (как в chat_pair_codes) остаётся и после
        # сожжения кода: фильтр итога прогона узнаёт по нему код закрытого
        # заказа, строки которого в chat_pair_codes уже нет.
        "add_executor_orders_code_hash",
        "ALTER TABLE executor_orders ADD COLUMN code_hash TEXT NOT NULL DEFAULT ''",
    ),
    (
        "idx_executor_orders_task",
        "CREATE INDEX IF NOT EXISTS idx_executor_orders_task "
        "ON executor_orders(task_id)",
    ),
    (
        # #1790: started_at пишут примитивы статуса, но только для работы,
        # начатой после выката. У задач, которые до него хоть раз побывали в
        # работе, момента начала нет и восстановить его нечем (#810): флаг
        # запрещает примитивам ставить сегодняшнюю дату на старую работу.
        "add_tasks_start_history_unknown",
        "ALTER TABLE tasks ADD COLUMN start_history_unknown INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # #1790: однократное заполнение — миграция применяется один раз
        # (запись в _migrations) и при старте нового процесса до поллера, пока
        # в базе лежат только строки старого кода. Draft исключён: до выката
        # в работе он не был, его первый running уже будет записан. Остальные
        # статусы, архив и завершённые включены: unarchive_task, return_to_work
        # и chat_pair.return_task_to_open возвращают старые задачи в работу.
        # Условие само идемпотентно: строка с проставленным started_at или уже
        # помеченная под него не попадает.
        "backfill_tasks_start_history_unknown",
        "UPDATE tasks SET start_history_unknown = 1 "
        "WHERE started_at IS NULL AND status != 'draft' "
        "AND start_history_unknown = 0",
    ),
    (
        # #1791: вид причины дефекта. NULL — причину никто не называл; это не
        # unknown (названо «не знаю») и не regression: у старых строк с
        # caused_by_task_id вид остаётся пустым и регрессией не объявляется.
        "add_tasks_cause_kind",
        "ALTER TABLE tasks ADD COLUMN cause_kind TEXT",
    ),
    (
        # #1825: первое использование токена сессии. Ставится при допуске
        # первого запроса с токеном (hub.services.chat_pair.resolve_session) и
        # необратимо. NULL — токен ни разу не предъявлен.
        "add_chat_pair_sessions_first_used_at",
        "ALTER TABLE chat_pair_sessions ADD COLUMN first_used_at TEXT",
    ),
    (
        # #1825: у сессий, выданных до этой миграции, «не использован» нечем
        # доказать — они считаются использованными. Иначе redeem_lost отозвал бы
        # живую работу, а повтор выкупа заменил бы токен работающего исполнителя.
        "backfill_chat_pair_sessions_first_used_at",
        "UPDATE chat_pair_sessions SET first_used_at = COALESCE(created_at, "
        "datetime('now')) WHERE first_used_at IS NULL",
    ),
    (
        # #1825: версия токена сессии растёт при каждой ротации повторного
        # выкупа; хост игнорирует ответ с меньшей версией.
        "add_chat_pair_sessions_token_version",
        "ALTER TABLE chat_pair_sessions ADD COLUMN token_version "
        "INTEGER NOT NULL DEFAULT 1",
    ),
    (
        # #1825: первый выкуп кода заказа хоста и окно безопасного повтора.
        # retry_until отсчитан от first_redeemed_at и не продлевается.
        "add_executor_orders_first_redeemed_at",
        "ALTER TABLE executor_orders ADD COLUMN first_redeemed_at TEXT",
    ),
    (
        "add_executor_orders_retry_until",
        "ALTER TABLE executor_orders ADD COLUMN retry_until TEXT",
    ),
    (
        # #1825: сессия, выданная выкупом этого заказа (строка chat_pair_sessions).
        "add_executor_orders_session_id",
        "ALTER TABLE executor_orders ADD COLUMN session_id INTEGER",
    ),
    (
        # #1825: число успешных ротаций токена повторным выкупом (лимит на заказ).
        "add_executor_orders_redeem_rotations",
        "ALTER TABLE executor_orders ADD COLUMN redeem_rotations "
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        # #1828: факт релизного мержа, записанный самим релизом. Строка пишется
        # сразу после успешного merge_pr — ДО чтения merge_commit_sha, поэтому
        # release_sha NULL-able: пока sha не прочитан, строка уже есть и свип
        # называет это отдельной причиной. Старых релизов в таблице нет.
        "create_release_merges",
        """CREATE TABLE IF NOT EXISTS release_merges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL,
            release_pr  INTEGER NOT NULL,
            release_sha TEXT,
            merged_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, release_pr)
        )""",
    ),
    (
        # #1829: какой текст навыка уже выдан зарегистрированной локальной
        # сессии. Запись — по (сессия, навык, версия, sha256 текста): смена
        # версии или текста даёт новую строку, и текст уходит снова. Старых
        # выдач в таблице нет — первая выдача после выката идёт полным текстом.
        "create_skill_deliveries",
        """CREATE TABLE IF NOT EXISTS skill_deliveries (
            session_id     TEXT NOT NULL,
            skill          TEXT NOT NULL,
            version        INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            delivered_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (session_id, skill, version, content_sha256)
        )""",
    ),
    (
        # #1817: последняя принятая фаза хоста (vm_starting, agent_running,
        # critic, submitting) и её время. В ленту фазы не пишутся.
        "add_executor_orders_phase",
        "ALTER TABLE executor_orders ADD COLUMN phase TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_executor_orders_phase_at",
        "ALTER TABLE executor_orders ADD COLUMN phase_at TEXT",
    ),
    (
        # #1817: код причины и текст причины из report хоста (reason ≤ 200).
        "add_executor_orders_report_reason_code",
        "ALTER TABLE executor_orders ADD COLUMN report_reason_code "
        "TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_executor_orders_report_reason",
        "ALTER TABLE executor_orders ADD COLUMN report_reason TEXT NOT NULL DEFAULT ''",
    ),
    (
        # #1817: когда закрытие заказа записало его итог в ленту (одна запись на
        # заказ; report хоста и возврат задачи своей записи не пишут).
        "add_executor_orders_final_entry_at",
        "ALTER TABLE executor_orders ADD COLUMN final_entry_at TEXT",
    ),
    (
        # #1817: report старого клиента (failed/rescued без reason_code) — это
        # `other`: иначе его идентичный повтор после выката стал бы 409.
        "backfill_executor_orders_report_reason_code",
        "UPDATE executor_orders SET report_reason_code = 'other' "
        "WHERE report_outcome IN ('failed', 'rescued') AND report_reason_code = ''",
    ),
    (
        # #1817: итоговую запись пишет только закрытие заказа. Уже закрытые заказы
        # помечены: поздних записей по ним не будет (допустимо: их итог уже в ленте
        # или в записях сдачи).
        "backfill_executor_orders_final_entry_at",
        "UPDATE executor_orders SET final_entry_at = COALESCE(closed_at, "
        "reported_at, created_at) WHERE final_entry_at IS NULL AND state = 'closed'",
    ),
    (
        # #1848: отпечаток проектного контракта CI (sha256 канонического JSON
        # {workflow_path, required_checks}), под которым вынесен вердикт
        # попытки. DEFAULT = отпечаток контракта хаба (ci.yml, четыре проверки):
        # ДО этой задачи другого контракта не было, поэтому вердикты, уже
        # стоящие в таблице, вынесены именно под ним — verified хаба остаются
        # verified, unverified и legacy не становятся verified. Новая
        # непроверенная претензия получает '' («нет отпечатка») из кода.
        "add_ci_report_attempts_contract_fp",
        "ALTER TABLE ci_report_attempts ADD COLUMN contract_fp "
        "TEXT NOT NULL DEFAULT '8f4dfc401a93a00850b2b78973c6ebd8c7614a35fe0f70b1ec82a448196400ca'",  # pragma: allowlist secret
    ),
    (
        "add_ci_run_reports_contract_fp",
        "ALTER TABLE ci_run_reports ADD COLUMN contract_fp "
        "TEXT NOT NULL DEFAULT '8f4dfc401a93a00850b2b78973c6ebd8c7614a35fe0f70b1ec82a448196400ca'",  # pragma: allowlist secret
    ),
]
