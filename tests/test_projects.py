from datetime import date

import pytest

from agent.actions import Actions
from agent.opportunities import Directory
from agent.projects import Picklists, onboarding_status, open_tasks, project_kinds, project_tools, propose_complete
from store.db import Store

DAVE = 500
TODAY = date(2026, 10, 11)
PICKLISTS = {
    "Tasks": [{"name": "status", "picklistValues": [{"value": "1", "label": "New"}, {"value": "5", "label": "Complete"},
                                                     {"value": "35", "label": "Engineer Assigned"}]}],
    "Projects": [{"name": "projectType", "picklistValues": [{"value": "3", "label": "Template"}, {"value": "5", "label": "Client"}]},
                 {"name": "status", "picklistValues": [{"value": "0", "label": "Inactive"}, {"value": "1", "label": "New"},
                                                       {"value": "5", "label": "Complete"}]}],
}
PROJECTS = [
    {"id": 1, "projectName": "TAG - NEW EMPLOYEE ONBOARDING - Riley Jones", "companyID": None, "status": 1,
     "projectType": 5, "endDateTime": "2026-06-04T00:00:00Z", "projectLeadResourceID": DAVE, "completedPercentage": 10},
    {"id": 2, "projectName": "Northwind Dental - MS ONBOARDING", "companyID": 2, "status": 1, "projectType": 5,
     "endDateTime": "2026-11-20T00:00:00Z", "projectLeadResourceID": 501, "completedPercentage": 60},
    {"id": 3, "projectName": "TAG - NEW EMPLOYEE ONBOARDING - BASE TEMPLATE", "companyID": None, "status": 1,
     "projectType": 5, "endDateTime": "2024-01-12T00:00:00Z", "projectLeadResourceID": DAVE, "completedPercentage": 0},
    {"id": 4, "projectName": "Template - Managed Services Onboarding", "companyID": None, "status": 1,
     "projectType": 3, "endDateTime": "2024-01-12T00:00:00Z", "projectLeadResourceID": None, "completedPercentage": 0},
    {"id": 5, "projectName": "Old Client - MS ONBOARDING", "companyID": 1, "status": 5, "projectType": 5,
     "endDateTime": "2025-01-01T00:00:00Z", "projectLeadResourceID": DAVE, "completedPercentage": 100},
]
TASKS = [
    {"id": 10, "projectID": 1, "title": "Schedule welcome lunch", "status": 35, "endDateTime": "2026-06-04T00:00:00Z", "assignedResourceID": DAVE},
    {"id": 11, "projectID": 1, "title": "Building tour", "status": 1, "endDateTime": "2026-06-04T00:00:00Z", "assignedResourceID": DAVE},
    {"id": 12, "projectID": 1, "title": "Order laptop", "status": 5, "endDateTime": "2026-06-01T00:00:00Z", "assignedResourceID": DAVE},
    {"id": 13, "projectID": 3, "title": "Template task", "status": 1, "endDateTime": "2024-01-12T00:00:00Z", "assignedResourceID": DAVE},
    {"id": 14, "projectID": 2, "title": "Deploy RMM agents", "status": 1, "endDateTime": "2026-10-01T00:00:00Z", "assignedResourceID": None},
    {"id": 15, "projectID": 2, "title": "Kickoff call", "status": 1, "endDateTime": "2026-11-01T00:00:00Z", "assignedResourceID": 501},
]


def matches(row, f):
    value = row.get(f["field"])
    return {"eq": lambda: value == f["value"], "noteq": lambda: value != f["value"], "in": lambda: value in f["value"],
            "contains": lambda: f["value"].lower() in (value or "").lower(), "exist": lambda: value is not None}[f["op"]]()


class FakeAutotask:
    def __init__(self):
        self.patches = []
        self.tables = {"Projects": PROJECTS, "Tasks": TASKS,
                       "Companies": [{"id": 1, "companyName": "Old Client"}, {"id": 2, "companyName": "Northwind Dental"}],
                       "Resources": [{"id": DAVE, "firstName": "Dave", "lastName": "Vener"},
                                     {"id": 501, "firstName": "Riley", "lastName": "Jones"}]}

    def _api_url(self, path):
        return path

    def _request_json(self, method, url, **kw):
        if method == "PATCH":
            self.patches.append((url, kw["json"]))
            return {"itemId": kw["json"]["id"]}
        return {"fields": PICKLISTS[url.split("/")[0]]}

    def query(self, entity, filters, include_fields=None, max_records=None):
        return [r for r in self.tables[entity] if all(matches(r, f) for f in filters)]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", str(DAVE))
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def setup():
    at = FakeAutotask()
    picks = Picklists(at)
    return at, Directory(at), picks, Actions(Store(":memory:"), project_kinds(at, picks), auto=set())


def test_daves_open_tasks_grouped_without_templates_or_done_ones():
    at, directory, picks, _ = setup()
    result = open_tasks(at, directory, picks, today=TODAY)
    assert result["person"] == "Dave Vener" and result["open"] == 2 and result["overdue"] == 2
    [group] = result["projects"]
    assert group["project"] == "TAG - NEW EMPLOYEE ONBOARDING - Riley Jones"
    assert {t["title"] for t in group["tasks"]} == {"Schedule welcome lunch", "Building tour"}
    assert group["tasks"][0]["due"] == "Jun 4, 2026" and group["tasks"][0]["status"] in ("New", "Engineer Assigned")


def test_someone_elses_tasks_and_project_filter():
    at, directory, picks, _ = setup()
    assert open_tasks(at, directory, picks, person="riley", today=TODAY)["open"] == 1
    assert open_tasks(at, directory, picks, project="dental", today=TODAY)["open"] == 0
    with pytest.raises(ValueError, match="No single Autotask user"):
        open_tasks(at, directory, picks, person="nobody", today=TODAY)


def test_onboarding_status_skips_templates_and_finished_projects():
    at, directory, picks, _ = setup()
    rows = onboarding_status(at, directory, picks, today=TODAY)["projects"]
    assert [r["project"] for r in rows] == ["Northwind Dental - MS ONBOARDING", "TAG - NEW EMPLOYEE ONBOARDING - Riley Jones"]
    dental, riley = rows
    assert dental["kind"] == "client" and dental["company"] == "Northwind Dental" and dental["lead"] == "Riley Jones"
    assert (dental["open_tasks"], dental["overdue_tasks"], dental["unassigned"]) == (2, 1, 1) and not dental["past_end"]
    assert riley["kind"] == "new employee" and riley["past_end"] and riley["percent"] == 10


def test_completing_tasks_waits_for_approval_and_respects_unticked_ones():
    at, directory, picks, actions = setup()
    slip = propose_complete(at, picks, actions, [10, 11, 12])  # 12 is already complete
    assert slip["status"] == "pending" and slip["pickable"] and not at.patches
    assert [i["subject"] for i in slip["items"]] == ["Schedule welcome lunch", "Building tour"]
    assert slip["summary"] == "Mark 2 tasks complete in TAG - NEW EMPLOYEE ONBOARDING - Riley Jones"
    done = actions.approve(slip["action_id"], decided_by="test", edits={"keep_ids": "11"})  # Building tour isn't done
    assert at.patches == [("Projects/1/Tasks", {"id": 10, "status": 5})]
    assert done["result"] == {"completed": 1, "skipped": 1, "failed": []}


def test_tools():
    at, directory, picks, actions = setup()
    tools = {t.spec.name: t for t in project_tools(at, directory, actions, picks)}
    assert set(tools) == {"find_tasks", "onboarding_status", "complete_tasks"}
    with pytest.raises(ValueError, match="already complete"):
        propose_complete(at, picks, actions, [12])


def test_onboarding_highlights_are_counted_in_code():
    at, directory, picks, _ = setup()
    h = onboarding_status(at, directory, picks, today=TODAY)["highlights"]
    assert h["live"] == 2 and h["client"] == 1
    assert h["past_planned_end"] == ["TAG - NEW EMPLOYEE ONBOARDING - Riley Jones (10% done, was due Jun 4, 2026)"]
    assert h["most_overdue_tasks"][0] == "TAG - NEW EMPLOYEE ONBOARDING - Riley Jones: 2 of 2 open tasks overdue"
    assert h["unassigned_tasks"] == ["Northwind Dental: 1"] and h["due_in_next_14_days"] == []
