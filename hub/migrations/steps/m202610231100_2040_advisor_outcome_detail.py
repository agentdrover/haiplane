"""#2040: причина неудачного применения approve стюарда.

``steward_judgements.advisor_outcome_detail`` - строка с кодом и причиной отказа,
когда исход применения ``failed`` (раньше причина оставалась только в логе).
Пусто - причины нет (прочие исходы и строки до выката).
"""

MIGRATIONS = [
    (
        "add_steward_judgements_advisor_outcome_detail",
        "ALTER TABLE steward_judgements ADD COLUMN advisor_outcome_detail TEXT "
        "NOT NULL DEFAULT ''",
    ),
]
