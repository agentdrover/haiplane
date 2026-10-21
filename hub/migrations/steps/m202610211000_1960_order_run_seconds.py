"""#1960: срок прогона заказа хосту, снимок при выпуске заказа.

``executor_orders.run_seconds`` - секунды жизни сессии implementer этого заказа
(от первого выкупа). Старые заказы получают 7200 - прежний общий TTL; выданные
сессии не меняются.
"""

MIGRATIONS = [
    (
        "add_executor_orders_run_seconds",
        "ALTER TABLE executor_orders ADD COLUMN run_seconds INTEGER NOT NULL DEFAULT 7200",
    ),
]
