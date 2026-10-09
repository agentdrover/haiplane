"""Критический путь и очередь с причинами по проекту (#1527).

Порядок исполнения жил в ``depends_on`` и в памяти стюарда: человек его не
видел, а страница проекта (#1528), входящие (#1501) и ``hub_my_context``
считали бы его каждый по-своему. Здесь один расчёт на всех, и он только
читает: ничего не пишет ни в задачу, ни в ленту.

Что считается

* Критический путь по каждому открытому эпику проекта — самая длинная по
  ВЕСУ цепочка незавершённых задач. Вес по размеру: XS=1, S=2, M=3, L=5,
  XL=8, без размера — 1. Это «шагов, не дней»: сроков здесь нет, пока
  cycle time ненадёжен (#525). Шаги человека (одобрение черновика, вердикт
  ревью, needs_decision, needs_info) стоят в цепочке с названием действия.
  Зависимость из другого проекта показана с его slug, путь через неё не
  обрывается. Доставленные предшественники в цепочку не входят.
* Узнать, что держит эпик: задача, которую транзитивно ждёт больше всего
  других задач цепочки (равенство — первая по порядку цепочки).
* Очередь: все незавершённые задачи проекта в топологическом порядке (при
  равенстве — priority, position, id, как у очереди оркестратора), по группам
  «ждёт вас / в работе / готово к старту / ждёт задачу / отложено», с одной
  причиной словами у каждой.

Чужих правил здесь нет

* Готовность зависимости — по ДОСТАВКЕ, тем же читателем, что у
  ``hub_list_dependencies`` и очереди (``delivery_state.blocker_delivery_cached``,
  #484/#885): «узнать не удалось» не снимает зависимость.
* «Следующая» — ответ ``orchestrator_queue.next_task`` (#1274): лист, DoR,
  зависимости доставлены, области свободны, WIP ниже лимита. Второго правила
  выбора нет; ему передаются уже посчитанные блокеры.
* Цикл в ``depends_on`` не роняет расчёт: он называется (``cycles``) и
  рассекается детерминированно — ребро, замкнувшее цикл при обходе по
  возрастанию id, в пути не учитывается, причина записана рядом.

Граф читается двумя запросами на весь хаб (задачи, рёбра) и считается в
памяти; на каждую зависимость, ещё не доставленную по записи мержа, идёт один
вопрос тому же читателю доставки.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import FINAL_STATUSES
from hub.services import orchestrator_queue as oq
from hub.services.delivery_state import CONTAINER_TYPES, blocker_delivery_cached

SIZE_WEIGHTS = {"XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8}
UNIT = "шагов, не дней"

GROUP_YOU = "waiting_you"
GROUP_WORK = "in_progress"
GROUP_READY = "ready"
GROUP_BLOCKED = "blocked"
GROUP_DEFERRED = "deferred"

GROUPS: tuple[tuple[str, str], ...] = (
    (GROUP_YOU, "Ждёт вас"),
    (GROUP_WORK, "В работе"),
    (GROUP_READY, "Готово к старту"),
    (GROUP_BLOCKED, "Ждёт задачу"),
    (GROUP_DEFERRED, "Отложено"),
)

_STATE_WORDS = {
    GROUP_YOU: "ваш шаг",
    GROUP_WORK: "в работе",
    GROUP_READY: "готово к старту",
    GROUP_BLOCKED: "ждёт задачу",
    GROUP_DEFERRED: "отложено",
}

_FINAL = {s.value for s in FINAL_STATUSES}

_WORK_REASONS = {
    "claimed": "взята, работа не начата",
    "running": "в работе",
    "ci_check": "ждёт CI",
    "fix_requested": "правки после ревью",
    "review": "на ревью: ждёт вердикта машины",
}

_TASKS_SQL = (
    "SELECT t.id, t.title, t.status, t.task_type, t.size, t.parent_id, "
    "t.project_id, t.archived, t.review_job_id, t.waiting_for, t.waiting_until, t.dor_passed, "
    "t.assigned_agent, t.priority, t.position, "
    "(SELECT COUNT(*) FROM pipeline_merges m WHERE m.task_id = t.id) AS merges "
    "FROM tasks t"
)


@dataclass
class Graph:
    """Граф хаба в памяти: задачи, рёбра и принадлежность проектам."""

    tasks: dict[int, dict[str, Any]]
    deps: dict[int, list[int]]
    slug_of: dict[int, str] = field(default_factory=dict)
    own: set[int] = field(default_factory=set)


@dataclass
class Reach:
    """Что нашёл обход от задач проекта по недоставленным зависимостям."""

    nodes: set[int] = field(default_factory=set)
    blockers: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    delivered_before: dict[int, list[int]] = field(default_factory=dict)


# --- чтение графа -------------------------------------------------------------


_resolver = oq.project_resolver


async def _load_graph(db: aiosqlite.Connection, project: Any) -> Graph:
    tasks = {int(r["id"]): dict(r) for r in await fetchall(db, _TASKS_SQL)}
    deps: dict[int, list[int]] = {}
    for row in await fetchall(
        db,
        "SELECT task_id, depends_on_task_id FROM task_dependencies "
        "ORDER BY task_id, depends_on_task_id",
    ):
        if int(row["depends_on_task_id"]) in tasks and int(row["task_id"]) in tasks:
            deps.setdefault(int(row["task_id"]), []).append(
                int(row["depends_on_task_id"])
            )
    project_of = _resolver(await repo.list_projects(db, include_archived=True))
    graph = Graph(tasks=tasks, deps=deps)
    for task_id, task in tasks.items():
        owner = project_of(tasks, task_id)
        graph.slug_of[task_id] = str(owner["slug"]) if owner is not None else ""
        if owner is not None and int(owner["id"]) == int(project["id"]):
            if not task["archived"]:
                graph.own.add(task_id)
    return graph


def _is_container(task: dict[str, Any]) -> bool:
    return str(task["task_type"]) in CONTAINER_TYPES


async def _delivery(db: aiosqlite.Connection, task: dict[str, Any]) -> dict[str, Any]:
    """Доставлена ли задача: ``delivered`` True / False / None (не узнать)."""
    if task["merges"]:
        return {"delivered": True, "reason": ""}
    entry = await repo.task_as_blocker(db, int(task["id"]))
    if entry is None:
        return {"delivered": None, "reason": "задача не найдена"}
    answer = await blocker_delivery_cached(db, entry)
    return {"delivered": answer.get("delivered"), "reason": answer.get("reason") or ""}


async def _reach(db: aiosqlite.Connection, graph: Graph) -> Reach:
    """Обход от незавершённых задач проекта по недоставленным зависимостям.

    Доставленная зависимость — граница: она в граф не входит и запоминается у
    зависящей. Недоставленная, из любого проекта, входит и обходится дальше:
    путь через чужую зависимость не обрывается.
    """
    reach = Reach()
    answers: dict[int, dict[str, Any]] = {}
    queue = sorted(
        t
        for t in graph.own
        if graph.tasks[t]["status"] not in _FINAL and not _is_container(graph.tasks[t])
    )
    reach.nodes.update(queue)
    while queue:
        node = queue.pop()
        for dep in graph.deps.get(node, []):
            if dep not in answers:
                answers[dep] = await _delivery(db, graph.tasks[dep])
            answer = answers[dep]
            if answer["delivered"] is True:
                reach.delivered_before.setdefault(node, []).append(dep)
                continue
            reach.blockers.setdefault(node, []).append(
                {
                    "task_id": dep,
                    "delivered": answer["delivered"],
                    "reason": answer["reason"],
                }
            )
            if dep not in reach.nodes:
                reach.nodes.add(dep)
                queue.append(dep)
    return reach


# --- циклы и порядок -----------------------------------------------------------


def _break_cycles(
    edges: dict[int, list[int]],
) -> tuple[set[tuple[int, int]], list[dict[str, Any]]]:
    """Найти циклы обходом в глубину и рассечь их; вернуть рассечённые рёбра.

    Обход итеративный (цепочка в сотни задач не упирается в рекурсию) и
    детерминированный: корни и соседи — по возрастанию id.
    """
    color: dict[int, int] = {}
    cut: set[tuple[int, int]] = set()
    cycles: list[dict[str, Any]] = []
    for root in sorted(edges):
        if color.get(root):
            continue
        color[root] = 1
        path = [root]
        stack = [(root, iter(sorted(edges.get(root, ()))))]
        while stack:
            node, neighbours = stack[-1]
            descended = False
            for nxt in neighbours:
                state = color.get(nxt, 0)
                if state == 1:
                    loop = [*path[path.index(nxt) :], nxt]
                    cycles.append(_cycle_entry(loop, node, nxt))
                    cut.add((node, nxt))
                elif state == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    descended = True
                    break
            if not descended:
                color[node] = 2
                path.pop()
                stack.pop()
    return cut, cycles


def _cycle_entry(loop: list[int], node: int, nxt: int) -> dict[str, Any]:
    named = " → ".join(f"#{t}" for t in loop)
    return {
        "tasks": loop,
        "cut": {"task_id": node, "depends_on": nxt},
        "reason": (
            f"цикл зависимостей: {named}; ребро #{node} → #{nxt} в пути не "
            "учитывается, чтобы расчёт дошёл до конца"
        ),
    }


def _topological(
    nodes: set[int],
    edges: dict[int, list[int]],
    tasks: dict[int, dict[str, Any]],
    key: Any = None,
) -> list[int]:
    """Зависимость раньше зависящей; при равенстве — priority, position, id.

    ``key`` (#1501) заменяет только равенство: входящие ранжируют черновики по
    #253, а не по очереди оркестратора. Топология — та же.
    """
    rank = key or oq._sort_key
    waiting = {n: len(edges.get(n, ())) for n in nodes}
    dependents: dict[int, list[int]] = {}
    for node, deps in edges.items():
        for dep in deps:
            dependents.setdefault(dep, []).append(node)
    heap = [(rank(tasks[n]), n) for n, c in waiting.items() if c == 0]
    heapq.heapify(heap)
    order: list[int] = []
    while heap:
        _, node = heapq.heappop(heap)
        order.append(node)
        for nxt in dependents.get(node, []):
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                heapq.heappush(heap, (rank(tasks[nxt]), nxt))
    return order


def order_nodes(
    nodes: set[int],
    edges: dict[int, list[int]],
    tasks: dict[int, dict[str, Any]],
    *,
    key: Any = None,
) -> tuple[list[int], dict[int, list[int]], list[dict[str, Any]]]:
    """Рассечь циклы и расставить узлы: ``(порядок, рёбра без разреза, циклы)``.

    Единственный расчёт порядка по ``depends_on`` (#1527): им пользуются
    ``compute`` и входящие (#1501). ``key`` — ранг внутри уровня.
    """
    cut, cycles = _break_cycles(edges)
    acyclic = {n: [d for d in deps if (n, d) not in cut] for n, deps in edges.items()}
    return _topological(nodes, acyclic, tasks, key), acyclic, cycles


# --- причины и группы -----------------------------------------------------------


def _human_step(task: dict[str, Any]) -> str:
    """Название действия человека; пусто — ждёт не человека."""
    status = str(task["status"])
    if status == "draft":
        return "одобрение черновика"
    if status == "review" and not task["review_job_id"]:
        return "вердикт ревью"
    return {
        "needs_decision": "решение (needs_decision)",
        "needs_info": "ответ на вопрос (needs_info)",
        "pending_report": "приёмка отчёта (pending_report)",
    }.get(status, "")


def _dep_label(task: dict[str, Any]) -> str:
    step = _human_step(task)
    if step:
        return f"ваш шаг: {step}"
    if task["status"] == "completed":
        return "ждёт доставки"
    return str(task["status"])


def _blocked_reason(
    graph: Graph, blockers: list[dict[str, Any]], current_slug: str
) -> str:
    parts = []
    for blocker in blockers:
        task = graph.tasks[blocker["task_id"]]
        tag = ""
        if graph.slug_of.get(task["id"]) != current_slug:
            tag = f" · проект {graph.slug_of.get(task['id']) or '?'}"
        parts.append(f"#{task['id']} ({_dep_label(task)}{tag})")
    return "ждёт " + ", ".join(parts)


def _classify(
    graph: Graph, task: dict[str, Any], blockers: list[dict[str, Any]], slug: str
) -> tuple[str, str]:
    """Группа и ОДНА причина словами."""
    status = str(task["status"])
    step = _human_step(task)
    if step:
        return GROUP_YOU, step
    if status in _FINAL:
        if status == "completed":
            why = (
                blockers[0]["reason"] if blockers else ""
            ) or "доставка не подтверждена"
            return GROUP_BLOCKED, f"ждёт доставки: {why}"
        return GROUP_BLOCKED, f"закрыта без доставки ({status})"
    if status in _WORK_REASONS:
        who = str(task["assigned_agent"] or "")
        suffix = f": {who}" if who and status == "running" else ""
        return GROUP_WORK, _WORK_REASONS[status] + suffix
    waiting = oq.current_wait(task)
    if waiting:
        return GROUP_DEFERRED, waiting
    if blockers:
        return GROUP_BLOCKED, _blocked_reason(graph, blockers, slug)
    if not task["dor_passed"]:
        return GROUP_BLOCKED, "DoR не пройден: задача не готова к старту"
    return GROUP_READY, "готова к старту"


def _state_word(task: dict[str, Any], group: str) -> str:
    if task["status"] in _FINAL:
        return "ждёт доставки"
    return _STATE_WORDS[group]


async def _demote_non_leaves(
    db: aiosqlite.Connection,
    graph: Graph,
    classes: dict[int, tuple[str, str]],
) -> None:
    """Не лист не стартуема: «ждёт подзадачи», а не «готово к старту».

    Признак тот же, что у очереди (``orchestrator_queue.not_leaf_reason``,
    #1455), второй копии нет; спрашивается только о тех, кто иначе был бы
    готов, — это единицы запросов.
    """
    for node, (group, _) in list(classes.items()):
        if group != GROUP_READY:
            continue
        why = await oq.not_leaf_reason(db, graph.tasks[node])
        if not why:
            continue
        children = await oq._open_children(db, node)
        listed = ", ".join(f"#{c}" for c in children)
        classes[node] = (
            GROUP_BLOCKED,
            f"ждёт подзадачи {listed}" if children else why,
        )


# --- критический путь -------------------------------------------------------------


def _weight(task: dict[str, Any]) -> int:
    return SIZE_WEIGHTS.get(str(task["size"] or ""), 1)


def _epic_of(graph: Graph, task_id: int) -> int | None:
    current = graph.tasks[task_id]["parent_id"]
    for _ in range(20):
        row = graph.tasks.get(current) if current is not None else None
        if row is None:
            return None
        if row["task_type"] == "epic":
            return int(row["id"])
        current = row["parent_id"]
    return None


def _closure(scope: set[int], edges: dict[int, list[int]]) -> set[int]:
    seen = set(scope)
    stack = list(scope)
    while stack:
        for dep in edges.get(stack.pop(), ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _longest_chain(
    subset: set[int],
    order: list[int],
    edges: dict[int, list[int]],
    tasks: dict[int, dict[str, Any]],
) -> list[int]:
    """Цепочка наибольшего веса; равенство — меньший id (детерминированно)."""
    best: dict[int, int] = {}
    via: dict[int, int | None] = {}
    for node in order:
        if node not in subset:
            continue
        choice: int | None = None
        for dep in sorted(edges.get(node, ())):
            if dep in best and (choice is None or best[dep] > best[choice]):
                choice = dep
        best[node] = _weight(tasks[node]) + (best[choice] if choice is not None else 0)
        via[node] = choice
    if not best:
        return []
    end = min(best, key=lambda n: (-best[n], n))
    chain: list[int] = []
    cursor: int | None = end
    while cursor is not None:
        chain.append(cursor)
        cursor = via[cursor]
    return chain[::-1]


def _bottleneck(
    subset: set[int],
    order: list[int],
    edges: dict[int, list[int]],
    chain: list[int],
) -> dict[str, Any] | None:
    """Задача, которую транзитивно ждёт больше всего других задач эпика."""
    index = {n: i for i, n in enumerate(n for n in order if n in subset)}
    dependents: dict[int, set[int]] = {n: set() for n in index}
    for node in reversed(list(index)):
        for dep in edges.get(node, ()):
            if dep in dependents:
                dependents[dep] |= {node} | dependents[node]
    ranked = [n for n in index if dependents[n]]
    if not ranked:
        return None
    place = {n: i for i, n in enumerate(chain)}
    top = min(ranked, key=lambda n: (-len(dependents[n]), place.get(n, len(chain)), n))
    return {"task_id": top, "waiting": len(dependents[top])}


def _step(
    graph: Graph,
    task_id: int,
    classes: dict[int, tuple[str, str]],
    slug: str,
) -> dict[str, Any]:
    task = graph.tasks[task_id]
    group, reason = classes[task_id]
    owner = graph.slug_of.get(task_id, "")
    return {
        "task_id": task_id,
        "title": task["title"],
        "status": task["status"],
        "size": task["size"],
        "weight": _weight(task),
        "project": owner,
        "foreign": owner != slug,
        "state": _state_word(task, group),
        "human_step": _human_step(task),
        "reason": reason,
    }


def _epic_entry(
    graph: Graph,
    epic_id: int,
    scope: set[int],
    ctx: _Calc,
) -> dict[str, Any]:
    subset = _closure(scope, ctx.edges)
    chain_ids = _longest_chain(subset, ctx.order, ctx.edges, graph.tasks)
    steps = [_step(graph, t, ctx.classes, ctx.slug) for t in chain_ids]
    weights = [s["weight"] for s in steps]
    total = sum(weights)
    epic = graph.tasks[epic_id]
    cycles = [c for c in ctx.cycles if subset & set(c["tasks"])]
    entry: dict[str, Any] = {
        "epic_id": epic_id,
        "title": epic["title"],
        "unit": UNIT,
        "weight": total,
        "weight_text": (
            f"вес {total} = {'+'.join(map(str, weights))} {UNIT}" if steps else ""
        ),
        "chain": steps,
        "bottleneck": _bottleneck(subset, ctx.order, ctx.edges, chain_ids),
        "delivered_before": sorted(ctx.reach.delivered_before.get(chain_ids[0], []))
        if chain_ids
        else [],
        "cycles": cycles,
        "note": _epic_note(steps),
    }
    return entry


def _epic_note(steps: list[dict[str, Any]]) -> str:
    if not steps:
        return "В эпике нет незавершённых задач"
    if len(steps) == 1:
        return "Цепочка из одного шага"
    return ""


@dataclass
class _Calc:
    """Общие входы расчёта одного проекта."""

    slug: str
    edges: dict[int, list[int]]
    order: list[int]
    classes: dict[int, tuple[str, str]]
    cycles: list[dict[str, Any]]
    reach: Reach


def _epics(graph: Graph, ctx: _Calc) -> list[dict[str, Any]]:
    scopes: dict[int, set[int]] = {}
    for task_id in graph.own:
        task = graph.tasks[task_id]
        if task["task_type"] == "epic" and task["status"] not in _FINAL:
            scopes.setdefault(task_id, set())
    for task_id in ctx.reach.nodes & graph.own:
        epic = _epic_of(graph, task_id)
        if epic in scopes:
            scopes[epic].add(task_id)
    return [_epic_entry(graph, e, scopes[e], ctx) for e in sorted(scopes)]


# --- очередь ---------------------------------------------------------------------


def _row(
    graph: Graph,
    task_id: int,
    classes: dict[int, tuple[str, str]],
    reach: Reach,
) -> dict[str, Any]:
    task = graph.tasks[task_id]
    group, reason = classes[task_id]
    return {
        "task_id": task_id,
        "title": task["title"],
        "status": task["status"],
        "size": task["size"],
        "priority": task["priority"],
        "group": group,
        "reason": reason,
        "human_step": _human_step(task),
        "waits_for": [b["task_id"] for b in reach.blockers.get(task_id, [])],
        "is_next": False,
    }


def _ready_reason(row: dict[str, Any], answer: dict[str, Any]) -> str:
    skipped = {s["task_id"]: s for s in answer["skipped"]}
    if row["task_id"] in skipped:
        return str(skipped[row["task_id"]]["detail"])
    if answer["next_task_id"] is not None:
        return f"готова, после #{answer['next_task_id']}"
    return str(answer["summary"])


def _queue_groups(
    graph: Graph, ctx: _Calc, answer: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    queued = [
        t
        for t in ctx.order
        if t in graph.own
        and graph.tasks[t]["status"] not in _FINAL
        and not _is_container(graph.tasks[t])
    ]
    rows = [_row(graph, t, ctx.classes, ctx.reach) for t in queued]
    ready = [r for r in rows if r["group"] == GROUP_READY]
    pick = answer["next_task_id"]
    chosen = next((r for r in ready if r["task_id"] == pick), None)
    for row in ready:
        row["reason"] = _ready_reason(row, answer)
    nxt: dict[str, Any]
    if chosen is not None:
        chosen["is_next"] = True
        chosen["reason"] = "следующая по очереди"
        ready.remove(chosen)
        ready.insert(0, chosen)
        nxt = {"task_id": pick, "title": chosen["title"], "reason": chosen["reason"]}
    else:
        reason = str(answer["summary"])
        if pick is not None:
            reason = f"хаб взял бы #{pick}, но она не в группе «готово к старту»"
        nxt = {"task_id": None, "title": "", "reason": reason}
    groups = []
    for key, label in GROUPS:
        members = (
            ready if key == GROUP_READY else [r for r in rows if r["group"] == key]
        )
        groups.append(
            {"key": key, "label": label, "count": len(members), "rows": members}
        )
    return groups, nxt


async def _delivered_count(
    db: aiosqlite.Connection, graph: Graph, days: int
) -> dict[str, Any]:
    rows = await fetchall(
        db,
        "SELECT DISTINCT task_id FROM pipeline_merges "
        "WHERE task_id IS NOT NULL AND merged_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    count = sum(1 for r in rows if int(r["task_id"]) in graph.own)
    return {"days": int(days), "count": count}


def _summary_line(groups: list[dict[str, Any]], nxt: dict[str, Any]) -> str:
    you = next(g for g in groups if g["key"] == GROUP_YOU)
    if you["rows"]:
        first = you["rows"][0]
        more = f" и ещё {you['count'] - 1}" if you["count"] > 1 else ""
        head = f"Сейчас ждёт вас: {first['human_step']} #{first['task_id']}{more}"
    else:
        head = "Сейчас вас никто не ждёт"
    if nxt["task_id"] is not None:
        return f"{head} · следующая по очереди: #{nxt['task_id']}"
    if any(g["count"] for g in groups):
        return f"{head} · следующей нет: {nxt['reason']}"
    return f"{head} · очередь пуста"


# --- вход -------------------------------------------------------------------------


async def compute(
    db: aiosqlite.Connection, project: Any, *, days: int = 30
) -> dict[str, Any]:
    """Путь и очередь одного проекта. Единственный расчёт; ничего не пишет."""
    slug = str(project["slug"])
    graph = await _load_graph(db, project)
    reach = await _reach(db, graph)
    edges = {n: [b["task_id"] for b in bl] for n, bl in reach.blockers.items()}
    order, edges, cycles = order_nodes(reach.nodes, edges, graph.tasks)
    classes = {
        n: _classify(graph, graph.tasks[n], reach.blockers.get(n, []), slug)
        for n in reach.nodes
    }
    await _demote_non_leaves(db, graph, classes)
    ctx = _Calc(
        slug=slug,
        edges=edges,
        order=order,
        classes=classes,
        cycles=cycles,
        reach=reach,
    )
    known = {n: reach.blockers.get(n, []) for n in graph.own}
    answer = await oq.next_task(db, project, known_blockers=known)
    groups, nxt = _queue_groups(graph, ctx, answer)
    return {
        "project": slug,
        "unit": UNIT,
        "summary": _summary_line(groups, nxt),
        "next": nxt,
        "epics": _epics(graph, ctx),
        "queue": {
            "groups": groups,
            "delivered": await _delivered_count(db, graph, days),
        },
        "cycles": cycles,
    }


# --- текст: один на CLI и MCP ----------------------------------------------------


HIDDEN_STEP = "(вне вашей сессии)"
HIDDEN_NEXT = "нет готовой или вне вашей сессии"


def _chain_text(chain: list[dict[str, Any]]) -> str:
    parts = []
    for step in chain:
        if step.get("hidden"):
            parts.append(HIDDEN_STEP)
            continue
        tag = f" (проект {step['project']})" if step["foreign"] else ""
        parts.append(f"#{step['task_id']}{tag}")
    return " → ".join(parts)


def _epic_lines(epic: dict[str, Any]) -> list[str]:
    head = f"Эпик #{epic['epic_id']} {epic['title']}"
    if not epic["chain"]:
        return [f"{head}: {epic['note']}"]
    lines = [f"{head}: {_chain_text(epic['chain'])} ({epic['weight_text']})"]
    if epic["note"]:
        lines.append(f"  {epic['note']}")
    for step in epic["chain"]:
        if step["human_step"]:
            lines.append(f"  ваш шаг: {step['human_step']} #{step['task_id']}")
    if epic["bottleneck"]:
        b = epic["bottleneck"]
        lines.append(f"  узкое место: #{b['task_id']}, ждут {b['waiting']}")
    lines.extend(f"  {c['reason']}" for c in epic["cycles"])
    return lines


def format_path(data: dict[str, Any]) -> list[str]:
    """Полный текст для CLI и MCP: итог, путь по эпикам, очередь по группам."""
    lines = [data["summary"], f"Вес в {data['unit']}; сроков нет.", ""]
    if not data["epics"]:
        lines.append("Открытых эпиков нет, критического пути нет")
    for epic in data["epics"]:
        lines.extend(_epic_lines(epic))
    lines.append("")
    for group in data["queue"]["groups"]:
        lines.append(f"{group['label']} ({group['count']})")
        for row in group["rows"]:
            mark = "▸" if row["is_next"] else " "
            lines.append(f" {mark} #{row['task_id']} {row['title']} — {row['reason']}")
    delivered = data["queue"]["delivered"]
    lines.append(f"Доставлено за {delivered['days']} дн.: {delivered['count']}")
    return lines


def format_path_brief(data: dict[str, Any], *, limit: int = 5) -> list[str]:
    """Блок «что дальше» для hub_my_context: следующая задача и пути эпиков."""
    nxt = data["next"]
    if nxt.get("scoped"):
        # Узкая сессия: только своя задача по номеру и названию, иначе пометка.
        first = (
            f"Next task: #{nxt['task_id']} {nxt['title']}"
            if nxt["task_id"] is not None
            else f"Next task: {HIDDEN_NEXT}"
        )
    elif nxt["task_id"] is not None:
        first = f"Next task: #{nxt['task_id']} {nxt['title']} — {nxt['reason']}"
    else:
        first = f"Next task: none — {nxt['reason']}"
    lines = [first]
    for epic in data["epics"][:limit]:
        if epic["chain"]:
            lines.append(
                f"Critical path, epic #{epic['epic_id']}: "
                f"{_chain_text(epic['chain'])} ({epic['weight_text']})"
            )
    if len(data["epics"]) > limit:
        lines.append(f"…и ещё эпиков: {len(data['epics']) - limit}")
    lines.append(f"Full view: oc-hub path {data['project']}")
    return lines


# --- блок для контекста задачи (#1643) ------------------------------------------

BRIEF_EPICS_FULL = 3
BRIEF_EPICS_SUMMARY = 1
# Потолок на расчёт блока: асинхронные чтения прерываются, чисто синхронный
# кусок (порядок узлов) — нет, поэтому главное средство — малое число запросов.
BRIEF_TIMEOUT_S = 3.0


def scope_to_visible(
    data: dict[str, Any], visible: set[int], breadcrumb_ids: set[int]
) -> dict[str, Any]:
    """Оставить в проекции только то, что вызывающая сессия и так видит (#1643).

    Сессия implementer привязана к одной задаче: ей видны эта задача, её
    предки, соседи и дети. Остальное исключается ДО форматирования: следующая
    задача, шаги пути и эпики вне видимости заменяются обезличенной пометкой, а
    чужие проекты не называются вовсе. Эпики берутся только из предков.
    """
    # Белый список полей: свободный текст (reason, note, summary) наружу не идёт —
    # в нём перечислены пропущенные кандидаты, их номера и причины.
    task_id = data["next"]["task_id"]
    shown = task_id is not None and task_id in visible
    nxt = {
        "task_id": task_id if shown else None,
        "title": data["next"]["title"] if shown else "",
        "scoped": True,
    }
    epics = []
    for epic in data["epics"]:
        if epic["epic_id"] not in breadcrumb_ids:
            continue
        chain = [
            {"task_id": step["task_id"], "foreign": False, "project": ""}
            if step["task_id"] in visible
            else {"task_id": None, "foreign": False, "project": "", "hidden": True}
            for step in epic["chain"]
        ]
        epics.append(
            {
                "epic_id": epic["epic_id"],
                "weight_text": epic["weight_text"],
                "chain": chain,
            }
        )
    return {"project": data["project"], "next": nxt, "epics": epics}


async def task_path_brief(
    db: aiosqlite.Connection,
    project: Any,
    breadcrumb: list[dict[str, Any]],
    *,
    summary: bool,
    visible: set[int] | None = None,
) -> dict[str, Any]:
    """Компактный блок «что дальше» для сессии одной задачи.

    Тот же расчёт и тот же текст, что у /path и CLI (``compute`` и
    ``format_path_brief``); сужены только эпики: сначала эпики предков задачи,
    затем остальные, всего не больше ``BRIEF_EPICS_*``. ``visible`` — границы
    видимости узкой сессии (implementer): None — полное чтение. Ничего не пишет.
    """
    if project is None:
        return {
            "status": "no_project",
            "lines": ["Путь проекта: у задачи нет проекта — блока «что дальше» нет"],
        }
    slug = str(project["slug"])
    try:
        data = await asyncio.wait_for(compute(db, project), BRIEF_TIMEOUT_S)
        own = {int(n["id"]) for n in breadcrumb}
        if visible is not None:
            data = scope_to_visible(data, visible, own)
        epics = sorted(data["epics"], key=lambda e: e["epic_id"] not in own)
        limit = BRIEF_EPICS_SUMMARY if summary else BRIEF_EPICS_FULL
        lines = format_path_brief({**data, "epics": epics}, limit=limit)
    except Exception as exc:  # noqa: BLE001 - блок необязателен, контекст важнее
        logging.getLogger(__name__).warning(
            "path brief of project %s not computed: %s: %s",
            slug,
            type(exc).__name__,
            exc,
        )
        return {
            "status": "unavailable",
            "project": slug,
            "lines": [f"Путь проекта не посчитан: {type(exc).__name__}"],
        }
    return {"status": "ok", "project": slug, "lines": lines}
