#!/usr/bin/env python3
"""oc-hub — CLI for agents on Pi to interact with OpenClaw Hub."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HUB_URL = os.environ.get("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")


def _api(method: str, path: str, body: Any | None = None) -> Any:
    """Call the Hub HTTP API.

    - ``body`` may be a dict OR a list (PUT for replace_acceptance_criteria
      sends a top-level array). Use ``None`` for GET/DELETE.
    - On HTTP error, try to render server JSON nicely (especially the
      ``dor_failed`` 422 detail from /approve), then exit 1.
    """
    url = f"{HUB_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if e.fp else ""
        _print_http_error(e.code, body_text)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(
            f"Connection error: {e.reason}\nIs openclaw-hub running at {HUB_URL}?",
            file=sys.stderr,
        )
        sys.exit(1)


def _print_http_error(code: int, body_text: str) -> None:
    """Pretty-print a Hub error body when it's structured JSON.

    Special-cases the DoR 422 detail so a human can act on it without
    parsing JSON by hand.
    """
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        print(f"HTTP {code}: {body_text}", file=sys.stderr)
        return

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and detail.get("error") == "dor_failed":
        print(
            f"HTTP {code}: DoR failed (score={detail.get('score')}, "
            f"task #{detail.get('task_id')})",
            file=sys.stderr,
        )
        missing = detail.get("missing_required") or []
        if missing:
            print("  Missing required: " + ", ".join(missing), file=sys.stderr)
        recs = detail.get("recommendations") or []
        if recs:
            print("  Top recommendations:", file=sys.stderr)
            for r in recs[:5]:
                print(
                    f"    - [{r.get('severity')}] {r.get('field')}: {r.get('message')}",
                    file=sys.stderr,
                )
        if detail.get("hint"):
            print(f"  Hint: {detail['hint']}", file=sys.stderr)
        return

    print(f"HTTP {code}: {body_text}", file=sys.stderr)


def _load_payload_file(path: str) -> dict[str, Any] | list[Any]:
    """Load a payload from a .json or .yaml/.yml file.

    YAML is optional: we try ``yaml`` if installed, otherwise fall back
    to JSON. Keeps the CLI usable without a hard YAML dependency.
    """
    p = Path(path)
    text = p.read_text()
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            print(
                "YAML file requested but PyYAML is not installed. "
                "Install with: uv add pyyaml",
                file=sys.stderr,
            )
            sys.exit(2)
        return yaml.safe_load(text)
    return json.loads(text)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_task_short(t: dict[str, Any]) -> None:
    src = f" [{t.get('source', 'human')}]" if t.get("source") == "agent" else ""
    tt = t.get("task_type", "task")
    tt_tag = f"[{tt}] " if tt != "task" else ""
    owner = f" [owner:{t.get('human_owner')}]" if t.get("human_owner") else ""
    reviewer = (
        f" [reviewer:{t.get('human_reviewer')}]" if t.get("human_reviewer") else ""
    )
    parent = f" (parent #{t['parent_id']})" if t.get("parent_id") else ""
    print(
        f"#{t['id']} {tt_tag}[{t['status']}] ({t.get('runtime', 'auto')}){src}{owner}{reviewer}{parent} {t['title']}"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_task(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "title": args.title,
        "description": args.description or "",
        "runtime": args.runtime,
        "source": "human",
        "run_immediately": args.run,
        "auto_review": not args.no_review,
        "task_type": getattr(args, "task_type", "task"),
        "priority": getattr(args, "priority", "medium"),
    }
    if getattr(args, "parent", None) is not None:
        body["parent_id"] = args.parent
    if getattr(args, "owner", None):
        body["human_owner"] = args.owner
    if getattr(args, "reviewer", None):
        body["human_reviewer"] = args.reviewer
    result = _api("POST", "/api/tasks", body)
    _print_json(result)
    return 0


def _cmd_create_typed(task_type: str) -> Any:
    """Factory for epic/feature/subtask commands."""

    def handler(args: argparse.Namespace) -> int:
        body: dict[str, Any] = {
            "title": args.title,
            "description": args.description or "",
            "task_type": task_type,
            "priority": getattr(args, "priority", "medium"),
            "source": "human",
        }
        if getattr(args, "parent", None) is not None:
            body["parent_id"] = args.parent
        if getattr(args, "owner", None):
            body["human_owner"] = args.owner
        if getattr(args, "reviewer", None):
            body["human_reviewer"] = args.reviewer
        result = _api("POST", "/api/tasks", body)
        _print_json(result)
        return 0

    return handler


def cmd_tree(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}/tree")
    _print_tree(result, indent=0)
    return 0


def _print_tree(node: dict[str, Any], indent: int = 0) -> None:
    prefix = "  " * indent
    tt = node.get("task_type", "task")
    status = node.get("status", "?")
    progress = node.get("progress")
    prog_str = ""
    if progress and progress.get("total", 0) > 0:
        prog_str = (
            f" ({progress['completed']}/{progress['total']} = {progress['percent']}%)"
        )
    print(f"{prefix}[{tt}] #{node['id']} {node['title']} — {status}{prog_str}")
    for child in node.get("children", []):
        _print_tree(child, indent + 1)


def cmd_context(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}/context")
    print(result.get("context_text", ""))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "title": args.title,
        "description": args.description or "",
        "source": "agent",
        "agent": args.agent or "",
        "rationale": args.rationale or "",
    }
    if getattr(args, "parent", None) is not None:
        body["parent_id"] = args.parent
    result = _api("POST", "/api/tasks", body)
    _print_json(result)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "comment": args.comment or "",
        "run": args.run,
        "force": getattr(args, "force", False),
    }
    if args.runtime:
        body["runtime"] = args.runtime
    result = _api("POST", f"/api/tasks/{args.task_id}/approve", body)
    _print_json(result)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"comment": args.comment or ""}
    result = _api("POST", f"/api/tasks/{args.task_id}/reject", body)
    _print_json(result)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.plan:
        body["plan"] = args.plan
    if args.runtime:
        body["runtime"] = args.runtime
    result = _api("POST", f"/api/tasks/{args.task_id}/start", body)
    _print_json(result)
    return 0


def cmd_question(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "agent": args.agent or "",
        "question": args.message,
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/question", body)
    _print_json(result)
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "answer": args.message,
        "resume": not args.no_resume,
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/answer", body)
    _print_json(result)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}")
    _print_json(result)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    params = f"?limit={args.limit}"
    if args.status:
        params += f"&status={args.status}"
    if getattr(args, "type", None):
        params += f"&type={args.type}"
    if getattr(args, "parent", None) is not None:
        params += f"&parent_id={args.parent}"
    if getattr(args, "owner", None):
        params += f"&human_owner={urllib.parse.quote(args.owner)}"
    if getattr(args, "reviewer", None):
        params += f"&human_reviewer={urllib.parse.quote(args.reviewer)}"
    result = _api("GET", f"/api/tasks{params}")
    for t in result:
        _print_task_short(t)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    result = _api(
        "POST",
        f"/api/tasks/{args.task_id}/updates",
        {
            "agent": args.agent or "",
            "kind": args.kind,
            "content": args.message,
        },
    )
    _print_json(result)
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}/updates")
    for u in result:
        print(f"[{u['created_at']}] ({u['kind']}) {u.get('agent', '')}: {u['content']}")
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    status_val = "draft" if not args.status or args.status == "pending" else args.status
    result = _api("GET", f"/api/tasks?status={status_val}&limit=50")
    for t in result:
        if t.get("source") == "agent":
            _print_task_short(t)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    action = "accept" if args.accept else "rework"
    body: dict[str, Any] = {
        "action": action,
        "instructions": args.message or "",
        "decision_summary": getattr(args, "summary", "") or "",
        "record_decision": getattr(args, "record_decision", False),
    }
    result = _api("POST", f"/api/tasks/{args.task_id}/decide", body)
    _print_json(result)
    return 0


def cmd_force_complete(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"comment": args.message or ""}
    result = _api("POST", f"/api/tasks/{args.task_id}/force-complete", body)
    _print_json(result)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    result = _api("GET", "/api/dashboard")
    _print_json(result)
    return 0


# ---------------------------------------------------------------------------
# Structured task form (#42): refine, ac, risk, readiness
# ---------------------------------------------------------------------------


# Map a CLI namespace to PATCH-payload keys. Lists are explicitly noted
# so we can keep the "user passed --scope-in" semantics: if the flag
# wasn't given at all, we don't include the key in the payload, and the
# field on the server stays untouched.
_REFINE_SCALAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("work_type", "work_type"),
    ("class_of_service", "class_of_service"),
    ("size", "size"),
    ("wip_tag", "wip_tag"),
    ("due_date", "due_date"),
    ("user_story", "user_story"),
    ("problem", "problem_statement"),
    ("value", "business_value"),
    ("tech_hints", "technical_hints"),
    ("human_owner", "human_owner"),
    ("human_reviewer", "human_reviewer"),
)
_REFINE_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("scope_in", "scope_in"),
    ("scope_out", "scope_out"),
    ("affected_area", "affected_areas"),
    ("validation", "validation_commands"),
    ("constraint", "constraints"),
    ("assumption", "assumptions"),
    ("out_of_scope_review", "out_of_scope_for_review"),
)


def _build_refine_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Assemble a TaskRefine payload from CLI args + --from-file overlay.

    Precedence (high to low): explicit CLI flags > --from-file contents.
    Any field not mentioned anywhere is omitted entirely (PATCH semantics
    — the server leaves untouched fields alone).
    """
    payload: dict[str, Any] = {}

    if getattr(args, "from_file", None):
        loaded = _load_payload_file(args.from_file)
        if not isinstance(loaded, dict):
            print(
                f"--from-file must contain a JSON/YAML object, got {type(loaded).__name__}",
                file=sys.stderr,
            )
            sys.exit(2)
        payload.update(loaded)

    for cli_attr, model_field in _REFINE_SCALAR_FIELDS:
        val = getattr(args, cli_attr, None)
        if val is not None:
            payload[model_field] = val
    for cli_attr, model_field in _REFINE_LIST_FIELDS:
        val = getattr(args, cli_attr, None)
        # action='append' yields None when the flag is never used and a
        # list (possibly empty after explicit append-to-empty trick) when it is.
        if val is not None:
            payload[model_field] = list(val)

    if getattr(args, "clear_acs", False):
        payload["acceptance_criteria"] = []

    return payload


def cmd_refine(args: argparse.Namespace) -> int:
    payload = _build_refine_payload(args)
    if not payload:
        print(
            "Nothing to refine. Pass at least one field flag or --from-file.",
            file=sys.stderr,
        )
        return 2
    result = _api("POST", f"/api/tasks/{args.task_id}/refine", payload)
    _print_json(result)
    return 0


# --- ac --------------------------------------------------------------------


def _print_acs(items: list[dict[str, Any]]) -> None:
    if not items:
        print("(no acceptance criteria)")
        return
    for ac in items:
        print(
            f"{ac['id']:<12} [{ac.get('verifiable_by', '?')}]\n"
            f"  Given: {ac.get('given', '')}\n"
            f"   When: {ac.get('when', '')}\n"
            f"   Then: {ac.get('then', '')}"
            + (f"\n   Test: {ac['test_ref']}" if ac.get("test_ref") else "")
        )


def cmd_ac_list(args: argparse.Namespace) -> int:
    result = _api("GET", f"/api/tasks/{args.task_id}/acceptance_criteria")
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_acs(result)
    return 0


def cmd_ac_add(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "id": args.id,
        "given": args.given,
        "when": args.when,
        "then": args.then,
        "verifiable_by": args.by,
    }
    if args.test_ref:
        body["test_ref"] = args.test_ref
    result = _api("POST", f"/api/tasks/{args.task_id}/acceptance_criteria", body)
    _print_json(result)
    return 0


def cmd_ac_delete(args: argparse.Namespace) -> int:
    ac_id = urllib.parse.quote(args.id, safe="")
    _api("DELETE", f"/api/tasks/{args.task_id}/acceptance_criteria/{ac_id}")
    print(f"Deleted AC {args.id} from task #{args.task_id}")
    return 0


def cmd_ac_replace(args: argparse.Namespace) -> int:
    loaded = _load_payload_file(args.from_file)
    if not isinstance(loaded, list):
        print(
            f"--from-file must contain a JSON/YAML array, got {type(loaded).__name__}",
            file=sys.stderr,
        )
        return 2
    result = _api("PUT", f"/api/tasks/{args.task_id}/acceptance_criteria", loaded)
    _print_json(result)
    return 0


# --- risk -------------------------------------------------------------------


def cmd_risk_list(args: argparse.Namespace) -> int:
    task = _api("GET", f"/api/tasks/{args.task_id}")
    risks = task.get("risks") or []
    if getattr(args, "json", False):
        _print_json(risks)
        return 0
    if not risks:
        print("(no risks)")
        return 0
    for r in risks:
        line = f"[{r.get('severity', '?')}] {r.get('kind', '?')}: {r.get('description', '')}"
        if r.get("mitigation"):
            line += f"  -> mitigation: {r['mitigation']}"
        print(line)
    return 0


def cmd_risk_add(args: argparse.Namespace) -> int:
    """Append a risk via PATCH /refine.

    TaskRefine.risks fully REPLACES the list, so we read-modify-write.
    There's a benign race window between GET and PATCH, but the CLI is
    interactive and the alternative (a dedicated endpoint) belongs to
    a future iteration.
    """
    task = _api("GET", f"/api/tasks/{args.task_id}")
    current = list(task.get("risks") or [])
    new_risk: dict[str, Any] = {
        "kind": args.kind,
        "severity": args.severity,
        "description": args.description,
        "mitigation": args.mitigation,
    }
    current.append(new_risk)
    result = _api("POST", f"/api/tasks/{args.task_id}/refine", {"risks": current})
    _print_json(result)
    return 0


# --- readiness --------------------------------------------------------------


_TEMPLATE_PACKAGE = "hub.cli_templates.work_types"


def _list_templates() -> list[str]:
    """Return sorted list of work_types that have a YAML template shipped.

    Reads ``hub/cli_templates/work_types/*.yaml`` via ``importlib.resources``
    so it works the same in editable installs and built wheels.
    """
    from importlib import resources

    files = resources.files(_TEMPLATE_PACKAGE)
    names = []
    for entry in files.iterdir():
        name = entry.name
        if name.endswith(".yaml") and not name.startswith("_"):
            names.append(name[:-5])
    return sorted(names)


def _read_template(work_type: str) -> str:
    """Read a packaged work_type template by name. Raises FileNotFoundError."""
    from importlib import resources

    files = resources.files(_TEMPLATE_PACKAGE)
    candidate = files.joinpath(f"{work_type}.yaml")
    if not candidate.is_file():
        raise FileNotFoundError(work_type)
    return candidate.read_text(encoding="utf-8")


def cmd_template(args: argparse.Namespace) -> int:
    """Render or save a YAML template for a given work_type.

    Two flows:
    - ``oc-hub template list``           — list available work_types.
    - ``oc-hub template <work_type>``    — print template to stdout.
    - ``oc-hub template <work_type> --out PATH``
                                          — write template to PATH (refuses
                                            to overwrite without --force).

    Templates are intentionally self-documenting (lots of comments) so
    Analyst-agents and humans can fill them in without a separate cheat-sheet.
    """
    if getattr(args, "list", False) or args.work_type == "list":
        names = _list_templates()
        if not names:
            print("No templates installed.", file=sys.stderr)
            return 1
        print("Available work_type templates:")
        for n in names:
            print(f"  - {n}")
        print(
            "\nUsage: oc-hub template <work_type> [--out PATH]\n"
            "Then: oc-hub refine <task_id> --from-file PATH"
        )
        return 0

    try:
        text = _read_template(args.work_type)
    except FileNotFoundError:
        print(
            f"Unknown work_type: {args.work_type!r}. "
            f"Available: {', '.join(_list_templates())}",
            file=sys.stderr,
        )
        return 2

    if args.out:
        out_path = Path(args.out)
        if out_path.exists() and not args.force:
            print(
                f"Refusing to overwrite {out_path} (pass --force to overwrite)",
                file=sys.stderr,
            )
            return 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote template to {out_path}")
        return 0

    sys.stdout.write(text)
    return 0


def cmd_readiness(args: argparse.Namespace) -> int:
    path = f"/api/tasks/{args.task_id}/readiness"
    if args.explain:
        path += "?explain=true"
    result = _api("GET", path)
    if args.json:
        _print_json(result)
        return 0

    print(
        f"Task #{args.task_id} readiness: score={result['score']} "
        f"dor_passed={'yes' if result['dor_passed'] else 'no'}"
    )
    missing = result.get("missing_required") or []
    if missing:
        print("  Missing required: " + ", ".join(missing))
    risks = result.get("risks") or []
    if risks:
        risk_brief = ", ".join(
            f"{r.get('kind')}:{r.get('severity')}" for r in risks[:5]
        )
        print(f"  Risks ({len(risks)}): {risk_brief}")
    recs = result.get("recommendations") or []
    blocking = [r for r in recs if r.get("severity") == "blocking"]
    if blocking:
        print("  Blocking recommendations:")
        for r in blocking[:5]:
            print(f"    - {r.get('field')}: {r.get('message')}")
    elif recs:
        print(f"  ({len(recs)} non-blocking suggestions; use --json for details)")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oc-hub", description="CLI for OpenClaw Hub")
    sub = parser.add_subparsers(dest="command", required=True)

    # task — create a task (open by default, or running with --run)
    p_task = sub.add_parser(
        "task", help="Create a task (open by default, --run to dispatch immediately)"
    )
    p_task.add_argument("--title", required=True)
    p_task.add_argument("--description", default="")
    p_task.add_argument(
        "--runtime", choices=["auto", "openrouter", "vast"], default="auto"
    )
    p_task.add_argument("--run", action="store_true", help="Dispatch immediately")
    p_task.add_argument(
        "--no-review",
        action="store_true",
        help="Disable automatic code review for this task",
    )
    p_task.add_argument(
        "--parent", type=int, default=None, help="Parent task ID (for hierarchy)"
    )
    p_task.add_argument(
        "--priority", choices=["critical", "high", "medium", "low"], default="medium"
    )
    p_task.add_argument("--owner", default="", help="Human owner of the task")
    p_task.add_argument("--reviewer", default="", help="Human reviewer of the task")
    p_task.set_defaults(func=cmd_task, task_type="task")

    # epic — create an epic
    p_epic = sub.add_parser("epic", help="Create an epic (top-level grouping)")
    p_epic.add_argument("--title", required=True)
    p_epic.add_argument("--description", default="")
    p_epic.add_argument(
        "--priority", choices=["critical", "high", "medium", "low"], default="medium"
    )
    p_epic.add_argument("--owner", default="", help="Human owner")
    p_epic.add_argument("--reviewer", default="", help="Human reviewer")
    p_epic.set_defaults(func=_cmd_create_typed("epic"))

    # feature — create a feature under an epic
    p_feature = sub.add_parser("feature", help="Create a feature (child of an epic)")
    p_feature.add_argument("--title", required=True)
    p_feature.add_argument("--description", default="")
    p_feature.add_argument("--parent", type=int, required=True, help="Parent epic ID")
    p_feature.add_argument(
        "--priority", choices=["critical", "high", "medium", "low"], default="medium"
    )
    p_feature.add_argument("--owner", default="", help="Human owner")
    p_feature.add_argument("--reviewer", default="", help="Human reviewer")
    p_feature.set_defaults(func=_cmd_create_typed("feature"))

    # subtask — create a subtask under a task
    p_subtask = sub.add_parser("subtask", help="Create a subtask (child of a task)")
    p_subtask.add_argument("--title", required=True)
    p_subtask.add_argument("--description", default="")
    p_subtask.add_argument("--parent", type=int, required=True, help="Parent task ID")
    p_subtask.add_argument(
        "--priority", choices=["critical", "high", "medium", "low"], default="medium"
    )
    p_subtask.add_argument("--owner", default="", help="Human owner")
    p_subtask.add_argument("--reviewer", default="", help="Human reviewer")
    p_subtask.set_defaults(func=_cmd_create_typed("subtask"))

    # tree — show hierarchy tree
    p_tree = sub.add_parser("tree", help="Show hierarchy tree for a task/epic/feature")
    p_tree.add_argument("task_id", type=int)
    p_tree.set_defaults(func=cmd_tree)

    # context — get agent work context
    p_context = sub.add_parser(
        "context", help="Get full work context (breadcrumb, siblings, progress)"
    )
    p_context.add_argument("task_id", type=int)
    p_context.set_defaults(func=cmd_context)

    # propose — agent proposes a task (creates draft)
    p_propose = sub.add_parser(
        "propose", help="Propose a task for human approval (creates draft)"
    )
    p_propose.add_argument("--title", required=True)
    p_propose.add_argument("--description", default="")
    p_propose.add_argument("--agent", default="")
    p_propose.add_argument("--rationale", default="")
    p_propose.add_argument("--parent", type=int, default=None, help="Parent task ID")
    p_propose.set_defaults(func=cmd_propose)

    # approve — approve a draft task
    p_approve = sub.add_parser("approve", help="Approve a draft task")
    p_approve.add_argument("task_id", type=int)
    p_approve.add_argument("--comment", default="")
    p_approve.add_argument(
        "--run", action="store_true", help="Also dispatch immediately"
    )
    p_approve.add_argument(
        "--runtime", choices=["auto", "openrouter", "vast"], default=None
    )
    p_approve.add_argument(
        "--force",
        action="store_true",
        help="Bypass the Definition of Ready gate (logged as alert)",
    )
    p_approve.set_defaults(func=cmd_approve)

    # reject — reject a draft task
    p_reject = sub.add_parser("reject", help="Reject a draft task")
    p_reject.add_argument("task_id", type=int)
    p_reject.add_argument("--comment", default="")
    p_reject.set_defaults(func=cmd_reject)

    # start — dispatch an open task
    p_start = sub.add_parser("start", help="Dispatch an open task")
    p_start.add_argument("task_id", type=int)
    p_start.add_argument(
        "--plan", default="", help="Work plan (required if no plan update exists)"
    )
    p_start.add_argument(
        "--runtime", choices=["auto", "openrouter", "vast"], default=None
    )
    p_start.set_defaults(func=cmd_start)

    # question — agent asks a question (sets needs_info)
    p_question = sub.add_parser(
        "question", help="Agent asks a question on a running task"
    )
    p_question.add_argument("task_id", type=int)
    p_question.add_argument("--message", required=True, help="The question text")
    p_question.add_argument("--agent", default="", help="Agent name")
    p_question.set_defaults(func=cmd_question)

    # answer — human answers a question (re-dispatches by default)
    p_answer = sub.add_parser("answer", help="Answer agent question and re-dispatch")
    p_answer.add_argument("task_id", type=int)
    p_answer.add_argument("--message", required=True, help="The answer text")
    p_answer.add_argument(
        "--no-resume", action="store_true", help="Don't re-dispatch, just save answer"
    )
    p_answer.set_defaults(func=cmd_answer)

    # status — get task details
    p_status = sub.add_parser("status", help="Get task status")
    p_status.add_argument("task_id", type=int)
    p_status.set_defaults(func=cmd_status)

    # list — list tasks
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument(
        "--status",
        choices=[
            "draft",
            "open",
            "running",
            "needs_info",
            "review",
            "fix_requested",
            "needs_decision",
            "completed",
            "failed",
            "rejected",
        ],
        default=None,
    )
    p_list.add_argument(
        "--type",
        choices=["epic", "feature", "task", "subtask"],
        default=None,
        help="Filter by task type",
    )
    p_list.add_argument("--parent", type=int, default=None, help="Filter by parent ID")
    p_list.add_argument("--owner", default=None, help="Filter by human_owner")
    p_list.add_argument("--reviewer", default=None, help="Filter by human_reviewer")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    # update — add status update to a task
    p_update = sub.add_parser("update", help="Add a status update or report to a task")
    p_update.add_argument("task_id", type=int)
    p_update.add_argument("--message", required=True, help="Update content")
    p_update.add_argument("--agent", default="", help="Agent name")
    p_update.add_argument(
        "--kind",
        default="status",
        choices=["status", "report", "blocker", "done", "review", "arbitration"],
        help="Update type",
    )
    p_update.set_defaults(func=cmd_update)

    # updates — list updates
    p_updates = sub.add_parser("updates", help="List updates for a task")
    p_updates.add_argument("task_id", type=int)
    p_updates.set_defaults(func=cmd_updates)

    # proposals — list agent proposals (backward compat)
    p_proposals = sub.add_parser("proposals", help="List agent proposals (draft tasks)")
    p_proposals.add_argument(
        "--status", choices=["pending", "approved", "rejected"], default=None
    )
    p_proposals.set_defaults(func=cmd_proposals)

    # decide — human decision after arbiter
    p_decide = sub.add_parser(
        "decide", help="Accept or rework a task after arbiter review"
    )
    p_decide.add_argument("task_id", type=int)
    group = p_decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true", help="Accept task as completed")
    group.add_argument("--rework", action="store_true", help="Send back for rework")
    p_decide.add_argument("--message", default="", help="Instructions for rework")
    p_decide.add_argument(
        "--summary",
        default="",
        help="Short summary/reason for the decision (recorded in task updates)",
    )
    p_decide.add_argument(
        "--record-decision",
        dest="record_decision",
        action="store_true",
        help="Also persist the decision through the notes integration",
    )
    p_decide.set_defaults(func=cmd_decide)

    # force-complete — human override of the completion gate
    p_force_complete = sub.add_parser(
        "force-complete",
        help="Force-complete a pending_report task without an agent done report (audited human override)",
    )
    p_force_complete.add_argument("task_id", type=int)
    p_force_complete.add_argument(
        "--message",
        default="",
        help="Reason for the override (recorded in audit trail)",
    )
    p_force_complete.set_defaults(func=cmd_force_complete)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Get dashboard data (JSON)")
    p_dash.set_defaults(func=cmd_dashboard)

    # ---- Structured task form (#42) -------------------------------------

    # refine — PATCH structured fields
    p_refine = sub.add_parser(
        "refine",
        help="Refine a task: PATCH structured fields (work_type, scope, value, ...)",
    )
    p_refine.add_argument("task_id", type=int)
    p_refine.add_argument(
        "--from-file",
        dest="from_file",
        help="Load defaults from a .json/.yaml file; CLI flags override",
    )
    p_refine.add_argument(
        "--work-type",
        dest="work_type",
        choices=[
            "feature",
            "bug",
            "refactor",
            "chore",
            "docs",
            "spike",
            "incident",
        ],
        default=None,
    )
    p_refine.add_argument(
        "--cos",
        dest="class_of_service",
        choices=["standard", "expedite", "fixed_date", "intangible"],
        default=None,
        help="Kanban class of service",
    )
    p_refine.add_argument("--size", choices=["XS", "S", "M", "L", "XL"], default=None)
    p_refine.add_argument(
        "--wip-tag",
        dest="wip_tag",
        choices=["feature_work", "bugfix", "tech_debt", "support"],
        default=None,
    )
    p_refine.add_argument(
        "--due", dest="due_date", default=None, help="Due date YYYY-MM-DD"
    )
    p_refine.add_argument(
        "--user-story",
        dest="user_story",
        default=None,
        help="As a <role> I want <X> so that <Y>",
    )
    p_refine.add_argument("--problem", dest="problem", default=None)
    p_refine.add_argument("--value", dest="value", default=None, help="Business value")
    p_refine.add_argument(
        "--tech-hints",
        dest="tech_hints",
        default=None,
        help="Technical hints / suggested approach",
    )
    p_refine.add_argument(
        "--scope-in",
        dest="scope_in",
        action="append",
        help="In-scope item (repeatable)",
    )
    p_refine.add_argument(
        "--scope-out",
        dest="scope_out",
        action="append",
        help="Out-of-scope item (repeatable)",
    )
    p_refine.add_argument(
        "--affected-area",
        dest="affected_area",
        action="append",
        help="Affected area / module (repeatable)",
    )
    p_refine.add_argument(
        "--validation",
        dest="validation",
        action="append",
        help="Validation command (repeatable)",
    )
    p_refine.add_argument(
        "--constraint",
        dest="constraint",
        action="append",
        help="Constraint (repeatable)",
    )
    p_refine.add_argument(
        "--assumption",
        dest="assumption",
        action="append",
        help="Assumption (repeatable)",
    )
    p_refine.add_argument(
        "--out-of-scope-review",
        dest="out_of_scope_review",
        action="append",
        help="Item to skip during code review (repeatable)",
    )
    p_refine.add_argument(
        "--owner",
        dest="human_owner",
        default=None,
        help="Human owner of the task",
    )
    p_refine.add_argument(
        "--reviewer",
        dest="human_reviewer",
        default=None,
        help="Human reviewer of the task",
    )
    p_refine.add_argument(
        "--clear-acs",
        dest="clear_acs",
        action="store_true",
        help="Drop all acceptance criteria for this task",
    )
    p_refine.set_defaults(func=cmd_refine)

    # ac — CRUD for acceptance criteria
    p_ac = sub.add_parser("ac", help="Manage acceptance criteria")
    ac_sub = p_ac.add_subparsers(dest="ac_command", required=True)

    p_ac_list = ac_sub.add_parser("list", help="List acceptance criteria for a task")
    p_ac_list.add_argument("task_id", type=int)
    p_ac_list.add_argument("--json", action="store_true", help="Print raw JSON")
    p_ac_list.set_defaults(func=cmd_ac_list)

    p_ac_add = ac_sub.add_parser("add", help="Add a single acceptance criterion")
    p_ac_add.add_argument("task_id", type=int)
    p_ac_add.add_argument("--id", required=True, help="AC id (e.g. AC-1)")
    p_ac_add.add_argument("--given", required=True)
    p_ac_add.add_argument("--when", required=True)
    p_ac_add.add_argument("--then", required=True)
    p_ac_add.add_argument(
        "--by",
        choices=["test", "manual", "log_check", "ui_check"],
        default="test",
        help="How this AC will be verified",
    )
    p_ac_add.add_argument("--test-ref", dest="test_ref", default="")
    p_ac_add.set_defaults(func=cmd_ac_add)

    p_ac_del = ac_sub.add_parser("delete", help="Delete an acceptance criterion")
    p_ac_del.add_argument("task_id", type=int)
    p_ac_del.add_argument("--id", required=True, help="AC id to delete")
    p_ac_del.set_defaults(func=cmd_ac_delete)

    p_ac_replace = ac_sub.add_parser(
        "replace",
        help="Atomically replace ALL ACs for a task from a JSON/YAML array file",
    )
    p_ac_replace.add_argument("task_id", type=int)
    p_ac_replace.add_argument(
        "--from-file",
        dest="from_file",
        required=True,
        help="Path to a JSON/YAML file containing the AC array",
    )
    p_ac_replace.set_defaults(func=cmd_ac_replace)

    # risk — list/add risks
    p_risk = sub.add_parser("risk", help="Manage task risks")
    risk_sub = p_risk.add_subparsers(dest="risk_command", required=True)

    p_risk_list = risk_sub.add_parser("list", help="List risks for a task")
    p_risk_list.add_argument("task_id", type=int)
    p_risk_list.add_argument("--json", action="store_true")
    p_risk_list.set_defaults(func=cmd_risk_list)

    p_risk_add = risk_sub.add_parser(
        "add",
        help="Append a risk to a task (read-modify-write via /refine)",
    )
    p_risk_add.add_argument("task_id", type=int)
    p_risk_add.add_argument(
        "--kind",
        required=True,
        choices=[
            "ambiguous_requirements",
            "large_scope",
            "external_dependency",
            "data_migration",
            "breaking_change",
            "security",
            "performance",
            "unknown_unknowns",
        ],
    )
    p_risk_add.add_argument(
        "--severity", required=True, choices=["low", "medium", "high"]
    )
    p_risk_add.add_argument("--description", required=True)
    p_risk_add.add_argument("--mitigation", required=True)
    p_risk_add.set_defaults(func=cmd_risk_add)

    # readiness — show DoR + score + recommendations
    p_readiness = sub.add_parser(
        "readiness", help="Show DoR / readiness score / recommendations"
    )
    p_readiness.add_argument("task_id", type=int)
    p_readiness.add_argument(
        "--explain",
        action="store_true",
        help="Include score-component breakdown in JSON output",
    )
    p_readiness.add_argument(
        "--json", action="store_true", help="Print full ReadinessReport JSON"
    )
    p_readiness.set_defaults(func=cmd_readiness)

    p_tpl = sub.add_parser(
        "template",
        help="Print or save a YAML template for a work_type (feature, bug, refactor, chore, docs, spike, incident)",
    )
    p_tpl.add_argument(
        "work_type",
        help="Work type to render. Use 'list' to see available templates.",
    )
    p_tpl.add_argument(
        "--out",
        help="Write template to this path instead of stdout.",
    )
    p_tpl.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the destination file if it already exists (use with --out).",
    )
    p_tpl.add_argument(
        "--list",
        action="store_true",
        help="List available templates (alias for 'oc-hub template list').",
    )
    p_tpl.set_defaults(func=cmd_template)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
