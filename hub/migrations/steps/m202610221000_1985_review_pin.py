"""#1985: пин отчёта ревью - заявленный sha и признак сверки с submissions.

Колонки пишет только ``review_pin.stamp`` в транзакции записи пинованного
отчёта. Старые строки и отчёты без пина: ``reviewed_sha=''``, ``pin_verified=0``
(пина не было - не значит «проверен»). Раннер терпит повтор не для ALTER, но
шаг записан в ``_migrations`` в той же транзакции, что и DDL.
"""

MIGRATIONS = [
    (
        "add_machine_reviews_reviewed_sha_1985",
        "ALTER TABLE machine_reviews ADD COLUMN reviewed_sha TEXT NOT NULL DEFAULT ''",
    ),
    (
        "add_machine_reviews_pin_verified_1985",
        "ALTER TABLE machine_reviews ADD COLUMN pin_verified INTEGER NOT NULL DEFAULT 0",
    ),
]
