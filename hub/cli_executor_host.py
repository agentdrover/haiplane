"""``oc-hub executor-host`` и ``oc-hub executor-launch`` (#1712).

Вынесено из ``hub/cli.py``: у того бюджет размера (#1669). Все команды —
человеку: хаб не пускает по этим маршрутам ни агента, ни ключ хоста.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any


def _api(*args: Any, **kwargs: Any) -> Any:
    # Лениво: hub.cli собирает парсеры при импорте и импортирует этот модуль.
    from hub.cli import _api as api

    return api(*args, **kwargs)


def _print_role_orders(result: Any) -> None:
    """#1973: role orders of the task as the hub built them; never recomputed here."""
    for view in (result.get("role_orders") if isinstance(result, dict) else None) or []:
        if view.get("unavailable"):
            print(f"Заказы ролей: {view['unavailable']}", file=sys.stderr)
        else:
            print(
                f"Роль: вызван {view.get('label')} (заказ {view.get('id')}, "
                f"{view.get('state')})",
                file=sys.stderr,
            )


def print_order_line(result: Any) -> None:
    """#1941: the line about the last host order for ``oc-hub status``; never recomputed here."""
    _print_role_orders(result)
    view = result.get("last_executor_order") if isinstance(result, dict) else None
    if not isinstance(view, dict):
        return
    waiting = view.get("waiting_memory")
    if view.get("unavailable"):
        print(f"Заказ хоста: {view['unavailable']}", file=sys.stderr)
    elif isinstance(waiting, dict) and waiting.get("line"):
        print(f"Заказ хоста {waiting['line']}", file=sys.stderr)  # #1972: строка хаба
    elif view.get("after_failed_run"):
        print(
            f"После неудачного прогона: класс {view.get('class')} "
            f"({view.get('class_name')})",
            file=sys.stderr,
        )


def print_critic_line(result: Any) -> None:
    """#1953: the last critic order line for ``oc-hub status``; never recomputed here."""
    view = result.get("critic_order") if isinstance(result, dict) else None
    if not isinstance(view, dict):
        return
    if view.get("unavailable"):
        print(f"Заказ критика: {view['unavailable']}", file=sys.stderr)
        return
    print(
        f"Заказ критика #{view.get('order_id')}: sha {str(view.get('sha'))[:12]} "
        f"{view.get('state')}/{view.get('outcome') or '-'}; {view.get('next_step')}",
        file=sys.stderr,
    )


def cmd_executor_host(args: argparse.Namespace) -> int:
    """add | disable | list — реестр хостов исполнителя."""
    if args.host_cmd == "list":
        result = _api("GET", "/api/admin/executor-hosts")
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        for host in result["hosts"]:
            state = "включён" if host["enabled"] else "отключён"
            print(
                f"{host['name']}\t{state}\tслотов {host.get('slots', 1)}\tprincipal #{host['principal_id']}"
            )
        return 0
    if args.host_cmd == "set-slots":
        name = urllib.parse.quote(args.name, safe="")
        result = _api(
            "POST", f"/api/admin/executor-hosts/{name}/slots", {"slots": args.slots}
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        print(
            f"{result['name']}: слотов {result['slots']}, незакрытых заказов {result['open_orders']}."
        )
        return 0
    name = urllib.parse.quote(args.name, safe="")
    if args.host_cmd == "disable":
        _api("POST", f"/api/admin/executor-hosts/{name}/disable")
        print(f"{args.name}: отключён; ключ отозван, коды сожжены, заказы закрыты.")
        return 0
    body = {
        "name": args.name,
        "public_key_pem": Path(args.public_key_file).read_text(),
        "expires_days": args.expires_days,
    }
    result = _api("POST", "/api/admin/executor-hosts", body)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Хост {result['name']} заведён (принципал #{result['principal_id']}).")
    print(f"Ключ API хоста (показан ОДИН раз, срок до {result['key_expires_at']}):")
    print(result["api_key"])
    return 0


def cmd_executor_launch(args: argparse.Namespace) -> int:
    """Запустить исполнителя: в облако (голова очереди) или на слот хоста."""
    body: dict[str, Any] = {"target": args.target}
    if args.task_id is not None:
        body["task_id"] = args.task_id
    slug = urllib.parse.quote(args.slug, safe="")
    result = _api("POST", f"/api/projects/{slug}/executor-launch", body)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if result.get("order_id"):
        print(
            f"#{result['task_id']}: заказ #{result['order_id']} выдан хосту "
            f"{result['host']}; код хост заберёт сам."
        )
    else:
        print(f"#{result['task_id']}: исполнитель запущен, агент {result['agent_id']}.")
    return 0


def add_executor_host_parsers(sub: Any) -> None:
    """Подкоманды executor-host и executor-launch; build_parser на потолке."""
    p_host = sub.add_parser(
        "executor-host", help="Executor hosts that receive their code from the hub"
    )
    host_sub = p_host.add_subparsers(dest="host_cmd", required=True)
    p_add = host_sub.add_parser("add", help="Register a host (human); key shown once")
    p_add.add_argument("name", help="host name, e.g. k8")
    p_add.add_argument(
        "--public-key-file", required=True, help="RSA-3072 public key, PEM SPKI"
    )
    p_add.add_argument("--expires-days", type=int, default=90)
    p_dis = host_sub.add_parser("disable", help="Disable a host (human)")
    p_dis.add_argument("name")
    host_sub.add_parser("list", help="List registered hosts")
    p_slots = host_sub.add_parser(
        "set-slots", help="Concurrent orders of a host, 1..4 (human)"
    )
    p_slots.add_argument("name")
    p_slots.add_argument("slots", type=int)
    for each in (p_add, p_slots, p_host):
        each.add_argument("--json", action="store_true", help="Print raw JSON")
    p_host.set_defaults(func=cmd_executor_host)

    p_launch = sub.add_parser(
        "executor-launch", help="Launch the executor on a task (human)"
    )
    p_launch.add_argument("slug", help="project slug")
    p_launch.add_argument(
        "--target", default="cloud", help="cloud (default) or slot:<host>"
    )
    p_launch.add_argument("--task-id", type=int, default=None)
    p_launch.add_argument("--json", action="store_true", help="Print raw JSON")
    p_launch.set_defaults(func=cmd_executor_launch)
