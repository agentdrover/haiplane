"""Turn dependency-audit findings into hub drafts instead of a merge block (#611).

The audit step used to fail the CI job, and the delivery gate (#605/#606) reads
that job to decide whether a PR may merge — so a single new advisory stopped
delivery for the WHOLE repository, without anyone committing anything. That
happened on 07.08.2026: PYSEC-2026-3552 appeared after develop's last green run,
PR #247 went red over a diff that had nothing to do with it, every task stalled,
and the fix needed a manual merge because a dependency bump has no task in the
hub. The decisive argument against keeping the block: an advisory may have no
fixed version at all, and then the freeze has no end.

So the audit stops being a gate — and that is only half a solution. A step that
can never fail is a step nobody reads, which trades a loud false stop for silent
rot. The other half is this script: every finding becomes a DRAFT TASK in the
hub, with a human owner and the normal approval gate, keyed by advisory id so
repeated runs neither duplicate nor conflict.

Two rules inherited from scripts/ci_report_to_hub.py, for the same reason:
never fail the job, and never report a guess — say what could not be done and
exit 0.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Why a draft and not a direct write: a task created with source=agent lands in
# ``draft`` (hub/services/lifecycle.py), so a human still decides whether the
# bump is worth doing. Verified by code rather than assumed: POST /api/tasks
# accepts source=agent from any authenticated identity — the gate there only
# stops an agent from claiming source=human (#360).
TASK_SOURCE = "agent"


def env_get(suffix: str) -> str:
    """HAIPLANE_ first, legacy OPENCLAW_ fallback; empty counts as unset.

    Standalone twin of ``hub.config.env_get`` — this script runs in CI
    without the hub package installed.
    """
    return (
        os.environ.get(f"HAIPLANE_{suffix}")
        or os.environ.get(f"OPENCLAW_{suffix}")
        or ""
    )


def log(msg: str) -> None:
    print(f"[hub-audit] {msg}", flush=True)


def hub_post(base: str, token: str, path: str, payload: dict) -> dict | None:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https from env
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        log(f"hub said {exc.code}: {body}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log(f"hub unreachable: {exc}")
    return None


def findings(report: dict) -> list[dict]:
    """Flatten pip-audit's JSON into one entry per (package, advisory).

    Tolerant of missing keys on purpose: the shape is pip-audit's, not ours, and
    a format change must degrade to "reported nothing, said so" rather than a
    traceback that turns this step red — the very thing the task removes.
    """
    out: list[dict] = []
    for dep in report.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "").strip()
        version = str(dep.get("version") or "").strip()
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            vuln_id = str(vuln.get("id") or "").strip()
            if not name or not vuln_id:
                continue
            fixes = [str(v) for v in (vuln.get("fix_versions") or [])]
            out.append(
                {
                    "id": vuln_id,
                    "package": name,
                    "version": version,
                    "fix_versions": fixes,
                    "description": str(vuln.get("description") or "").strip(),
                }
            )
    return out


def draft_payload(finding: dict) -> dict:
    """The task body for one advisory — STABLE across runs by construction.

    Nothing here may vary between runs of the same advisory: the hub's
    idempotency refuses a known client_request_id that arrives with a different
    payload, so a run number, branch or timestamp would turn the second PR of
    the day into a 409 instead of silence. That is why the description carries
    the advisory's own facts only.
    """
    fixes = ", ".join(finding["fix_versions"]) or "исправленной версии пока нет"
    where = f"{finding['package']} {finding['version']}".strip()
    body = (
        f"Аудит зависимостей нашёл {finding['id']} в {where}.\n\n"
        f"Исправленные версии: {fixes}.\n\n"
        "Аудит больше не блокирует доставку (#611), поэтому эта задача — "
        "единственный след находки: без неё уязвимость осталась бы строкой в "
        "логе, которую никто не читает.\n\n"
    )
    if finding["description"]:
        body += f"Из advisory: {finding['description'][:1500]}"
    return {
        "title": f"Уязвимость {finding['id']} в {finding['package']}: обновить зависимость",
        "description": body.strip(),
        "source": TASK_SOURCE,
        "rationale": (
            "Найдено шагом аудита в CI. Обновление зависимости — работа, "
            "у которой должен быть владелец и маршрут доставки."
        ),
        "human_owner": "Denis Pukinov",
        "human_reviewer": "Denis Pukinov",
        # Dedup key: one draft per advisory, whatever branch or run saw it.
        "client_request_id": f"pip-audit:{finding['id']}",
    }


def main() -> int:
    base = env_get("HUB_URL").rstrip("/")
    token = env_get("HUB_CI_TOKEN")
    report_path = Path(os.environ.get("AUDIT_JSON") or "audit.json")

    if not report_path.exists():
        log(f"no audit report at {report_path} — nothing to report")
        return 0
    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log(f"audit report unreadable ({exc}) — reporting nothing")
        return 0
    if not isinstance(report, dict):
        log("audit report is not an object — reporting nothing")
        return 0

    found = findings(report)
    if not found:
        log("no vulnerabilities — nothing to file")
        return 0

    if not base or not token:
        log(
            f"{len(found)} vulnerability(ies) found but HAIPLANE_HUB_URL / "
            "HAIPLANE_HUB_CI_TOKEN (or legacy OPENCLAW_*) are not configured — "
            "printing them here and "
            "filing nothing: " + ", ".join(f["id"] for f in found)
        )
        return 0

    for finding in found:
        result = hub_post(base, token, "/api/tasks", draft_payload(finding))
        if result is None:
            log(f"{finding['id']}: not filed (see the reason above)")
            continue
        log(
            f"{finding['id']} in {finding['package']} → hub task "
            f"#{result.get('id')} ({result.get('status')})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
