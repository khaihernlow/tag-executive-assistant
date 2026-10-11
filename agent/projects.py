"""Autotask project tasks: Dave's open tasks, onboarding progress, ticking tasks off.

Mostly new-employee and client onboarding. When this was built Dave had 62
open tasks, 35 of them in RJ Gray's onboarding checklist, all due months ago
and many plainly done ("Building tour", "Schedule Welcome Lunch"). Templates
live in Autotask as projects (type Template), with tasks dated 2024; they're
never counted.

Reads and writes both go to the Autotask API (projects and tasks aren't in
Matt's tag_data, and ticking a task off must be current). Completing tasks
waits on an approval card where Dave can untick any that aren't done.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.calendar import local_zone
from agent.opportunities import Directory, dave_resource_id
from agent.tools import Tool
from llm.provider import ToolSpec

COMPLETE_KIND = "complete_tasks"
TASK_FIELDS = ["id", "projectID", "title", "status", "endDateTime", "assignedResourceID"]
PROJECT_FIELDS = ["id", "projectName", "companyID", "status", "projectType", "endDateTime",
                  "projectLeadResourceID", "completedPercentage"]


class Picklists:
    """Task statuses and project types/statuses, by label, cached."""

    def __init__(self, at: Any) -> None:
        self.at, self._cache = at, {}

    def labels(self, entity: str, field: str) -> dict[int, str]:
        if (entity, field) not in self._cache:
            fields = self.at._request_json("GET", self.at._api_url(f"{entity}/entityInformation/fields"))["fields"]
            f = next((x for x in fields if x["name"] == field), {})
            self._cache[(entity, field)] = {int(v["value"]): v["label"] for v in f.get("picklistValues") or []
                                            if str(v["value"]).lstrip("-").isdigit()}
        return self._cache[(entity, field)]

    def value(self, entity: str, field: str, label: str) -> int:
        return next(k for k, v in self.labels(entity, field).items() if v.lower() == label.lower())


def _real(project: dict[str, Any], picks: Picklists) -> bool:
    """Not a template (by type, or by name for older ones filed as Client)."""
    name = (project.get("projectName") or "").upper()
    return (picks.labels("Projects", "projectType").get(project.get("projectType")) != "Template"
            and "TEMPLATE" not in name and "DO NOT USE" not in name)


def _day(value: str | None) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %d, %Y").replace(" 0", " ") if value else ""


def open_tasks(at: Any, directory: Directory, picks: Picklists, project: str = "", person: str = "",
               today: date | None = None) -> dict[str, Any]:
    """Open tasks for Dave (or someone else), grouped by project, templates left out."""
    today = today or datetime.now(local_zone()).date()
    who = dave_resource_id()
    if person:
        who = directory.resource_by_name(person) or 0
        if not who:
            raise ValueError(f"No single Autotask user matches '{person}'.")
    complete = picks.value("Tasks", "status", "Complete")
    tasks = at.query("Tasks", [{"op": "eq", "field": "assignedResourceID", "value": who},
                               {"op": "noteq", "field": "status", "value": complete}], TASK_FIELDS)
    projects = {p["id"]: p for p in at.query("Projects", [{"op": "in", "field": "id",
                                                            "value": sorted({t["projectID"] for t in tasks}) or [0]}],
                                             PROJECT_FIELDS)}
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    status = picks.labels("Tasks", "status")
    for t in tasks:
        p = projects.get(t["projectID"])
        if not p or not _real(p, picks):
            continue
        if project and project.lower() not in p["projectName"].lower():
            continue
        due = (t.get("endDateTime") or "")[:10]
        groups[t["projectID"]].append({
            "task_id": t["id"], "project_id": t["projectID"], "title": (t.get("title") or "").strip(),
            "status": status.get(t["status"], str(t["status"])), "due": _day(t.get("endDateTime")),
            "overdue": bool(due) and due < today.isoformat(),
        })
    out = [{"project_id": pid, "project": projects[pid]["projectName"].strip(),
            "company": directory.company_name(projects[pid].get("companyID")) if projects[pid].get("companyID") else "",
            "tasks": sorted(items, key=lambda i: (i["due"] == "", i["due"]))}
           for pid, items in groups.items()]
    out.sort(key=lambda g: -len(g["tasks"]))
    return {"person": directory.resource_name(who), "open": sum(len(g["tasks"]) for g in out),
            "overdue": sum(i["overdue"] for g in out for i in g["tasks"]),
            "highlights": {"by_project": [f"{g['project']}: {len(g['tasks'])} open, "
                                          f"{sum(i['overdue'] for i in g['tasks'])} overdue" for g in out[:4]]},
            "projects": out}


def onboarding_status(at: Any, directory: Directory, picks: Picklists, today: date | None = None) -> dict[str, Any]:
    """Every live onboarding project (client and new-employee), with progress and what's overdue."""
    today = today or datetime.now(local_zone()).date()
    pstatus = picks.labels("Projects", "status")
    done_states = {picks.value("Projects", "status", "Complete"), picks.value("Projects", "status", "Inactive")}
    projects = [p for p in at.query("Projects", [{"op": "contains", "field": "projectName", "value": "onboard"}],
                                    PROJECT_FIELDS)
                if p["status"] not in done_states and _real(p, picks)]
    complete = picks.value("Tasks", "status", "Complete")
    tasks = at.query("Tasks", [{"op": "in", "field": "projectID", "value": [p["id"] for p in projects] or [0]},
                               {"op": "noteq", "field": "status", "value": complete}], TASK_FIELDS)
    by_project: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for t in tasks:
        by_project[t["projectID"]].append(t)
    directory.prefetch_companies([p["companyID"] for p in projects if p.get("companyID")])
    rows = []
    for p in projects:
        open_ = by_project.get(p["id"], [])
        end = (p.get("endDateTime") or "")[:10]
        rows.append({
            "project_id": p["id"], "project": p["projectName"].strip(),
            "kind": "new employee" if "EMPLOYEE" in p["projectName"].upper() else "client",
            "company": directory.company_name(p["companyID"]) if p.get("companyID") else "",
            "status": pstatus.get(p["status"], str(p["status"])), "percent": p.get("completedPercentage") or 0,
            "planned_end": _day(p.get("endDateTime")), "past_end": bool(end) and end < today.isoformat(),
            "lead": directory.resource_name(p.get("projectLeadResourceID")) if p.get("projectLeadResourceID") else "",
            "open_tasks": len(open_),
            "overdue_tasks": sum(1 for t in open_ if (t.get("endDateTime") or "9")[:10] < today.isoformat()),
            "unassigned": sum(1 for t in open_ if not t.get("assignedResourceID")),
        })
    rows.sort(key=lambda r: (r["kind"], r["planned_end"] == "", r["planned_end"]))
    name = lambda r: r["company"] or r["project"]  # noqa: E731
    return {
        "highlights": {
            "live": len(rows), "client": sum(r["kind"] == "client" for r in rows),
            "past_planned_end": [f"{name(r)} ({r['percent']}% done, was due {r['planned_end']})" for r in rows if r["past_end"]],
            "most_overdue_tasks": [f"{name(r)}: {r['overdue_tasks']} of {r['open_tasks']} open tasks overdue"
                                   for r in sorted(rows, key=lambda r: -r["overdue_tasks"])[:3] if r["overdue_tasks"]],
            "unassigned_tasks": [f"{name(r)}: {r['unassigned']}" for r in sorted(rows, key=lambda r: -r["unassigned"])
                                 if r["unassigned"]][:3],
            "due_in_next_14_days": [f"{name(r)} ({r['percent']}% done, due {r['planned_end']})" for r in rows
                                    if not r["past_end"] and r["planned_end"] and
                                    (datetime.strptime(r["planned_end"], "%b %d, %Y").date() - today).days <= 14],
        },
        "projects": rows,
    }


# ── ticking tasks off ────────────────────────────────────────────────────────

def propose_complete(at: Any, picks: Picklists, actions: Actions, task_ids: list[int], note: str = "") -> dict[str, Any]:
    ids = sorted({int(i) for i in task_ids})
    if not ids:
        raise ValueError("Give the task_ids to mark complete (from find_tasks).")
    rows = at.query("Tasks", [{"op": "in", "field": "id", "value": ids}], TASK_FIELDS)
    complete = picks.value("Tasks", "status", "Complete")
    rows = [t for t in rows if t["status"] != complete]
    if not rows:
        raise ValueError("Those tasks are already complete, or don't exist.")
    projects = {p["id"]: p["projectName"].strip() for p in at.query(
        "Projects", [{"op": "in", "field": "id", "value": sorted({t["projectID"] for t in rows})}], ["id", "projectName"])}
    items = [{"id": str(t["id"]), "project_id": t["projectID"], "subject": (t.get("title") or "").strip(),
              "from": projects.get(t["projectID"], ""), "reason": f"due {_day(t.get('endDateTime'))}" if t.get("endDateTime") else ""}
             for t in rows]
    where = projects[rows[0]["projectID"]] if len(projects) == 1 else f"{len(projects)} projects"
    summary = f"Mark {len(items)} task{'s' if len(items) != 1 else ''} complete in {where}"
    return public_action(actions.propose(COMPLETE_KIND, summary, {
        "items": items, "pickable": True, "pick_note": "Untick any that aren't done yet.", "keep_ids": "",
        "note": note.strip()}))


def project_kinds(at: Any, picks: Picklists) -> list[ActionKind]:
    def complete(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        skip = {s for s in (payload.get("keep_ids") or "").split(",") if s}
        status = picks.value("Tasks", "status", "Complete")
        done, failed = [], []
        for item in payload["items"]:
            if item["id"] in skip:
                continue
            try:
                # Tasks belong to a project: Autotask updates them through it.
                at._request_json("PATCH", at._api_url(f"Projects/{item['project_id']}/Tasks"),
                                 json={"id": int(item["id"]), "status": status})
                done.append(item["id"])
            except Exception as e:  # noqa: BLE001 - one bad task shouldn't stop the rest
                failed.append({"id": item["id"], "error": str(e)[:200]})
        return {"completed": len(done), "skipped": len(skip), "failed": failed}

    return [ActionKind(COMPLETE_KIND, complete, editable=("keep_ids",))]


def project_tools(at: Any, directory: Directory, actions: Actions, picks: Picklists) -> list[Tool]:
    def pending_note(result: dict[str, Any]) -> dict[str, Any]:
        if result["status"] == "pending":
            result["note"] = "Not saved yet. Dave sees an approval card; tell him it's ready for his approval."
        return result

    return [
        Tool(spec=ToolSpec(
            name="find_tasks",
            description=("Open Autotask project tasks assigned to Dave (or person), grouped by project, overdue flagged, "
                         "templates left out. Mostly onboarding checklists. Filter by project name words. Dave sees the full list on a card: reply with a short summary (totals, the few that stand out), not the list again."),
            input_schema={"type": "object", "properties": {
                "project": {"type": "string", "description": "Words in the project name, e.g. 'RJ Gray'"},
                "person": {"type": "string", "description": "Someone other than Dave"}}}),
            handler=lambda a: open_tasks(at, directory, picks, a.get("project") or "", a.get("person") or "")),
        Tool(spec=ToolSpec(
            name="onboarding_status",
            description="Every live onboarding project (client and new-employee): percent done, planned end, lead, "
                        "open and overdue tasks. Dave sees the full list on a card: reply with a short summary (totals, the few that stand out), not the list again.",
            input_schema={"type": "object", "properties": {}}),
            handler=lambda a: onboarding_status(at, directory, picks)),
        Tool(spec=ToolSpec(
            name="complete_tasks",
            description=("Propose marking Autotask tasks complete (Dave approves, and can untick any first). "
                         "Use task_ids from find_tasks."),
            input_schema={"type": "object", "properties": {
                "task_ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["task_ids"]}),
            handler=lambda a: pending_note(propose_complete(at, picks, actions, a.get("task_ids") or []))),
    ]
