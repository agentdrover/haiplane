"""#2139: триггер человеческого возврата не пишет запись при пометке «не ошибка пары».

Человек в вердикте может поставить ``pair_miss=false`` с заметкой (значение
лежит в payload события ``review_verdict_recorded``). Запись
``human_changes_requested`` не создаётся ТОЛЬКО при JSON false: отсутствие поля,
null, строка, число и любое другое значение - запись, как раньше. Предикат тот
же, что в ``steward_exit._human_returns``. Существующие записи не трогаются.
"""

MIGRATIONS = [
    (
        "drop_trigger_false_approve_human_return_2139",
        "DROP TRIGGER IF EXISTS trg_false_approve_human_return",
    ),
    (
        "create_trigger_false_approve_human_return_pair_miss_2139",
        """CREATE TRIGGER IF NOT EXISTS trg_false_approve_human_return
        AFTER INSERT ON events
        WHEN NEW.kind = 'review_verdict_recorded' AND NEW.task_id IS NOT NULL
          AND NEW.actor NOT IN ('hub', 'policy', 'steward')
          AND json_extract(NEW.payload, '$.verdict') = 'changes_requested'
          AND COALESCE(json_type(NEW.payload, '$.pair_miss'), '') != 'false'
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
]
