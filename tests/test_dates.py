from datetime import date

from agent.dates import date_guide, relative
from agent.tools import Tool, ToolRegistry
from llm.provider import ToolCall, ToolSpec

TODAY = date(2026, 10, 11)  # a Sunday


def test_relative_days():
    assert [relative(date(2026, 10, d), TODAY) for d in (11, 12, 10, 16, 5)] == \
        ["today", "tomorrow", "yesterday", "in 5 days", "6 days ago"]
    assert relative(date(2026, 11, 1), TODAY) == "in 3 weeks"
    assert relative(date(2026, 1, 1), TODAY) == "9 months ago"


def test_guide_covers_every_date_format_the_tools_use():
    result = {"quotes": [{"expires": "Oct 12, 2026"}, {"expires": "Oct 12, 2026"}],
              "slot": {"start": "2026-10-13T10:00:00-04:00"}, "events": [{"start": "Wed Oct 14 02:00 PM"}],
              "note": "Created Jan 1, 2026", "id": 12345}
    assert date_guide(result, TODAY) == {
        "Oct 12, 2026": "Monday, tomorrow", "2026-10-13": "Tuesday, in 2 days",
        "Wed Oct 14": "Wednesday, in 3 days", "Jan 1, 2026": "Thursday, 9 months ago"}


def test_registry_adds_the_guide_without_changing_the_data(monkeypatch):
    monkeypatch.setattr("agent.tools.today_local", lambda: TODAY)
    monkeypatch.setattr("agent.tools.date_guide", lambda r: date_guide(r, TODAY))
    registry = ToolRegistry([Tool(spec=ToolSpec(name="t", description="", input_schema={}),
                                  handler=lambda a: {"expires": "Oct 12, 2026"})])
    import json
    out = json.loads(registry.run(ToolCall(id="1", name="t", input={}))[0])
    assert out["expires"] == "Oct 12, 2026"
    assert out["dates"] == {"today": "Sunday, Oct 11, 2026", "Oct 12, 2026": "Monday, tomorrow"}
