"""#2122: проект запуска заказа хосту, снимок при выпуске заказа.

``executor_orders.project_slug`` - slug проекта, которым выпущен заказ (тот, с кем
``_chosen_task`` сверил владельца задачи). Хост видит его в ``GET …/orders`` как
``project`` и может отказаться до выкупа кода. Не пересчитывается при отдаче.
Заказы, выпущенные до миграции, остаются с NULL.
"""

MIGRATIONS = [
    (
        "add_executor_orders_project_slug_2122",
        "ALTER TABLE executor_orders ADD COLUMN project_slug TEXT",
    ),
]
