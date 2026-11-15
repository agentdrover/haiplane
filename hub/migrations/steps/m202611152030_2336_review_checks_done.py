"""#2336: список проверок ревьюера и текст его облачного прогона на строке отчёта.

``checks_done`` — JSON-список ``[{what, how, result}]``; ``'[]'`` у каждой строки
до миграции и у отчёта без списка: «список не приложен», а не «проверок не было».
``run_text`` — финальный текст облачного прогона после ``scrub_secrets`` и с
потолком длины; ``''`` — текст не сохранён (старая строка, прогон ещё идёт,
провайдер не ответил), а не «прогон ничего не написал». Пишет только свип
диспетчей ревью; колонки не читаются ни одним решением о вердикте.
"""

MIGRATIONS = [
    (
        "add_machine_reviews_checks_done_2336",
        "ALTER TABLE machine_reviews ADD COLUMN checks_done TEXT NOT NULL DEFAULT '[]'",
    ),
    (
        "add_machine_reviews_run_text_2336",
        "ALTER TABLE machine_reviews ADD COLUMN run_text TEXT NOT NULL DEFAULT ''",
    ),
]
