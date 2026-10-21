"""Курсор ролей (#1973) для тестов очистки событий.

Миграция ставит курсор заказов ролей на конец ленты, и очистка не трогает
события за ним. Тест, который проверяет саму очистку, сперва «дочитывает» ленту
так же, как это делает свип ролей, и только потом чистит.
"""

from __future__ import annotations


async def read_the_feed(db) -> None:
    await db.execute(
        "UPDATE role_orders_cursor SET cursor_event_id = "
        "(SELECT COALESCE(MAX(id), 0) FROM events)"
    )
    await db.commit()
