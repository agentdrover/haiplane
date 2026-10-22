"""#1943: метка времени ДЕЙСТВИЯ, выбранного исходом рекомендации.

``executor_triage_recommendations.followup_action_at`` - время самого долговечного
действия (``executor_orders.created_at``, ``submissions.submitted_at`` или срок
72 часа), по которому свип выбрал ``followup``. ``followup_at`` остаётся временем
обработки. Суточный замер дайджеста берёт метку действия, поэтому не зависит от
того, когда прошёл свип. Только ADD COLUMN: таблицу пересобирает миграция #1947.
Строки с исходом до колонки несут NULL; дайджест читает их по ``followup_at``.
"""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "add_executor_triage_recommendations_followup_action_at",
        "ALTER TABLE executor_triage_recommendations ADD COLUMN followup_action_at TEXT",
    ),
]
