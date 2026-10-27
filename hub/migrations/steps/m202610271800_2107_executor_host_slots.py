"""#2107: несколько слотов исполнителя на хост (k8 человек ставит в 2 командой set-slots).

``executor_hosts.slots`` — новый заказ (или переоткрытие) хост получает, пока
незакрытых заказов меньше; по умолчанию 1, как было. Снижение ``slots`` открытые
заказы не закрывает (триггеры смотрят только INSERT, переоткрытие и перенос незакрытого заказа на
другой хост). Уникальный индекс «один незакрытый заказ на хост»
(#1712, пересоздан в #1972) снимается, а гарантия на уровне базы остаётся:
триггеры не дают вставить, переоткрыть или перенести на хост заказ сверх ``slots`` хоста. Проверка
в брони (``executor_orders._slot_busy``) называет причину; триггер ловит гонку
двух соединений (ошибка целостности, как прежде у индекса).

Пересоздание ``executor_orders`` (как в #1972) уносит триггеры вместе с таблицей:
такая миграция должна создать их заново.
"""

MIGRATIONS = [
    (
        "add_executor_hosts_slots_2107",
        "ALTER TABLE executor_hosts ADD COLUMN slots INTEGER NOT NULL DEFAULT 1",
    ),
    (
        "drop_idx_executor_orders_one_open_per_host_2107",
        "DROP INDEX IF EXISTS idx_executor_orders_one_open_per_host",
    ),
    (
        "idx_executor_orders_host_state_2107",
        "CREATE INDEX IF NOT EXISTS idx_executor_orders_host_state "
        "ON executor_orders(host, state)",
    ),
    (
        "trg_executor_orders_slots_insert_2107",
        "CREATE TRIGGER IF NOT EXISTS trg_executor_orders_slots_insert "
        "BEFORE INSERT ON executor_orders "
        "WHEN NEW.state <> 'closed' AND (SELECT COUNT(*) FROM executor_orders o "
        "WHERE o.host = NEW.host AND o.state <> 'closed') >= COALESCE((SELECT h.slots "
        "FROM executor_hosts h WHERE h.name = NEW.host), 1) "
        "BEGIN SELECT RAISE(ABORT, 'executor host slots are full'); END",
    ),
    (
        "trg_executor_orders_slots_reopen_2107",
        "CREATE TRIGGER IF NOT EXISTS trg_executor_orders_slots_reopen "
        "BEFORE UPDATE OF state, host ON executor_orders "
        "WHEN NEW.state <> 'closed' AND (OLD.state = 'closed' OR OLD.host <> NEW.host) "
        "AND (SELECT COUNT(*) FROM executor_orders o WHERE o.host = NEW.host "
        "AND o.state <> 'closed') >= COALESCE((SELECT h.slots FROM executor_hosts h "
        "WHERE h.name = NEW.host), 1) "
        "BEGIN SELECT RAISE(ABORT, 'executor host slots are full'); END",
    ),
]
