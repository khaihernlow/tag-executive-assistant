from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import lunch as L
from agent.mealeo import Menu, parse_item, parse_menu
from llm.provider import LLMResponse
from store.db import Store

TZ = ZoneInfo("America/New_York")
WED = datetime(2026, 10, 14, tzinfo=TZ)

MENU_PAGE = """
<div data-category-name="Popular"><a data-load-url="/menu/1/20" class="menu-item"><div class="menu-desc wrap"><h4 title="06 BLT">06 BLT</h4>
<h5><span class="price">$12.57</span></h5></div></a></div>
<div data-category-name="Salad"><a data-load-url="/menu/1/10" class="menu-item"><div class="menu-desc wrap"><h4 title="05 Grilled Chkn Salad">05 Grilled Chkn Salad</h4>
<h5><span class="price">$16.64</span></h5></div></a></div>
<div data-category-name="Deli Sandwiches"><a data-load-url="/menu/1/20" class="menu-item"><div class="menu-desc wrap"><h4 title="06 BLT">06 BLT</h4>
<h5><span class="price">$12.57</span></h5></div></a>
<a data-load-url="/menu/1/30" class="menu-item"><div class="menu-desc wrap"><h4 title="01 Tuna Salad Sandwich">01 Tuna Salad Sandwich</h4>
<h5><span class="price">$13.71</span></h5></div></a></div>
"""


def option(name, price=0):
    return f'<label><input type="checkbox" data-incprice="{price}" ><i></i> <span>{name}</span></label>'


SALAD_PAGE = ('<div data-base-price="18.50"></div>'
              '<div class="m-b-20 options-block" data-id="1" data-min="0" data-limit="0"><h4>Salad Dressing</h4>'
              + option("Balsamic") + option("Honey Mustard") + '</div>'
              '<div class="m-b-20 options-block" data-id="2" data-min="0" data-limit="0"><h4>Salad Add Meat</h4>'
              + option("Add Add Grilled Chicken (+$4.25)", "4.25") + '</div><h4>Special Requests</h4>')
BLT_PAGE = ('<div data-base-price="14.00"></div>'
            '<div class="m-b-20 options-block isLimited" data-id="3" data-min="0" data-limit="2"><h4>Sandwich Bread</h4>'
            + option("White Bread") + option("Wheat Bread") + '</div>'
            '<div data-id="4" class="isReq options-block stillReq"><h4 class="required-selection"> Sandwich Sides <span></span></h4>'
            '<p>Required.</p>' + option("Macaroni Salad") + option("Coleslaw") + '</div>'
            '<div class="m-b-20 options-block" data-id="5" data-min="0" data-limit="0"><h4>Sandwich Dressing</h4>'
            + option("Lettuce") + option("Tomato") + option("Onion") + option("Mayo") + '</div>'
            '<div class="m-b-20 options-block" data-id="6" data-min="0" data-limit="0"><h4>Sandwich Adds</h4>'
            + option("Add Add Swiss Cheese (+$1.25)", "1.25") + option("Add Add Avocado (+$2.50)", "2.50") + '</div>')
TUNA_PAGE = '<div data-base-price="15.25"></div>' + BLT_PAGE.split("</div>", 1)[1].replace("14.00", "15.25")
PAGES = {"/restaurants": MENU_PAGE, "/menu/1/10": SALAD_PAGE, "/menu/1/20": BLT_PAGE, "/menu/1/30": TUNA_PAGE}


class Session:
    def __init__(self):
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        text = next(v for k, v in PAGES.items() if k in url)
        return type("R", (), {"text": text, "raise_for_status": lambda self: None})()


def tool(name, **args):
    return LLMResponse(content=[{"type": "tool_use", "id": "t", "name": name, "input": args}], stop_reason="tool_use")


class LLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, *args, **kwargs):
        return self.responses.pop(0)


class Graph:
    mailbox = "dvener@tag.example"

    def __init__(self, meeting=True):
        self.meeting = meeting

    def get_all(self, path, params=None, limit=50, headers=None):
        events = [{"subject": "Lunch", "start": {"dateTime": "2026-10-14T12:00:00"}}]
        if self.meeting:
            events.append({"subject": "Weekly Leadership Team Meeting", "start": {"dateTime": "2026-10-14T12:00:00"}})
        return events


class Teams:
    def __init__(self):
        self.sent, self.inbox = [], {}

    def send(self, upn, text):
        self.sent.append((upn, text))

    def replies(self, upn, since):
        return [r for r in self.inbox.get(upn, []) if r["at"] > since]


def at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return WED.replace(hour=h, minute=m)


def utc(hhmm):
    return at(hhmm).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.500Z")


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("LUNCH_LIVE", "1")
    store = Store(":memory:")
    menu = Menu(store, url="https://www.mealeo.com/restaurants/x", session=Session())
    return store, menu, Teams()


def test_menu_reads_items_once_and_prices_from_the_items_own_page():
    items = parse_menu(MENU_PAGE)
    assert [(i.name, i.section, i.price) for i in items] == [
        ("05 Grilled Chkn Salad", "Salad", 16.64), ("06 BLT", "Deli Sandwiches", 12.57), ("01 Tuna Salad Sandwich", "Deli Sandwiches", 13.71)]
    base, groups = parse_item(BLT_PAGE)
    assert base == 14.0
    assert [(g.name, g.min, g.limit) for g in groups] == [("Sandwich Bread", 0, 2), ("Sandwich Sides", 1, 1),
                                                          ("Sandwich Dressing", 0, 0), ("Sandwich Adds", 0, 0)]
    assert [(c.name, c.price) for c in groups[3].choices] == [("Add Swiss Cheese", 1.25), ("Add Avocado", 2.5)]


def test_a_normal_wednesday(setup):
    store, menu, teams = setup
    llm = LLM(
        tool("read_lunch_reply", status="order", item="01 Tuna Salad Sandwich", wants=["wheat", "coleslaw"]),   # Dan
        tool("pick_options", options=["Wheat Bread", "Coleslaw"], unavailable=[]),
    )
    lunch = L.Lunch(Graph(), teams, llm, store, menu)

    plan = lunch.tick(at("08:00"))
    assert plan["status"] == "planned" and plan["meeting"]["subject"] == "Weekly Leadership Team Meeting"
    assert plan["people"]["Dave"]["line"] == {"item": "05 Grilled Chkn Salad", "options": ["Honey Mustard"], "price": 18.5}
    assert teams.sent == []                                    # nobody asked before 9:30

    lunch.tick(at("09:31"))
    assert [u for u, _ in teams.sent] == ["dlawrence@tagsolutions.com", "dpetersen@tagsolutions.com"]
    assert "reply by 10:45" in teams.sent[0][1]

    teams.inbox["dlawrence@tagsolutions.com"] = [{"text": "tuna on wheat with coleslaw please", "at": utc("09:40")}]
    plan = lunch.tick(at("09:42"))
    assert plan["people"]["Dan"]["state"] == "ordered"
    assert plan["people"]["Dan"]["line"] == {"item": "01 Tuna Salad Sandwich", "options": ["Wheat Bread", "Coleslaw"], "price": 15.25}
    assert teams.sent[-1] == ("dlawrence@tagsolutions.com", "Got it: 01 Tuna Salad Sandwich (Wheat Bread, Coleslaw), $15.25. Ordering at 11.")

    lunch.tick(at("10:31"))                                    # Dom is reminded, once
    lunch.tick(at("10:34"))
    assert [u for u, t in teams.sent if "reminder" in t] == ["dpetersen@tagsolutions.com"]

    plan = lunch.tick(at("10:46"))                             # deadline: Dom gets his backup
    dom = plan["people"]["Dom"]
    assert dom["state"] == "backup" and dom["line"]["price"] == 14.0 + 1.25 + 2.5
    assert plan["total"] == round(18.5 + 18.5 + 15.25 + 17.75, 2)
    # Everything's in order, but the first orders wait for Dave's go before checkout
    assert plan["status"] == "needs_dave"
    assert plan["reasons"] == ["one of the first orders, so it waits for your go before checkout"]


def test_unclear_replies_get_a_question_and_out_means_out(setup):
    store, menu, teams = setup
    llm = LLM(tool("read_lunch_reply", status="unclear", question="Which sandwich would you like?"),
              tool("read_lunch_reply", status="out"))
    lunch = L.Lunch(Graph(), teams, llm, store, menu)
    lunch.tick(at("09:30"))
    teams.inbox["dlawrence@tagsolutions.com"] = [{"text": "the usual", "at": utc("09:35")}]
    lunch.tick(at("09:36"))
    assert teams.sent[-1] == ("dlawrence@tagsolutions.com", "Which sandwich would you like?")
    teams.inbox["dlawrence@tagsolutions.com"].append({"text": "actually skip me today", "at": utc("09:50")})
    plan = lunch.tick(at("09:51"))
    assert plan["people"]["Dan"]["state"] == "out" and teams.sent[-1][1] == "Got it, no lunch for you this week."


def test_an_item_not_on_the_menu_is_asked_about_not_guessed(setup):
    store, menu, teams = setup
    llm = LLM(tool("read_lunch_reply", status="order", item="Cheeseburger", wants=[]))
    lunch = L.Lunch(Graph(), teams, llm, store, menu)
    lunch.tick(at("09:30"))
    teams.inbox["dpetersen@tagsolutions.com"] = [{"text": "cheeseburger", "at": utc("09:35")}]
    plan = lunch.tick(at("09:36"))
    assert plan["people"]["Dom"]["state"] == "clarifying"
    assert "\"Cheeseburger\" isn't on the menu" in teams.sent[-1][1]


def test_no_meeting_on_the_calendar_means_no_order_unless_dave_says_so(setup):
    store, menu, teams = setup
    lunch = L.Lunch(Graph(meeting=False), teams, LLM(), store, menu)
    assert lunch.tick(at("09:31"))["status"] == "no_meeting"
    assert teams.sent == []
    L.order_anyway(store, at("09:40"))
    lunch.tick(at("09:41"))
    assert len(teams.sent) == 2                                 # now Dan and Dom are asked


def test_dry_run_sends_nothing_but_shows_what_it_would_say(setup, monkeypatch):
    monkeypatch.delenv("LUNCH_LIVE")
    store, menu, teams = setup
    lunch = L.Lunch(Graph(), teams, LLM(), store, menu)
    lunch.tick(at("09:31"))
    assert teams.sent == []
    view = L.today_view(store, at("09:32"))
    assert view["live"] is False and [m["to"] for m in view["messages"]] == ["Dan", "Dom"]
    assert all(m["sent"] is False for m in view["messages"])


def test_only_wednesday_mornings(setup):
    store, menu, teams = setup
    lunch = L.Lunch(Graph(), teams, LLM(), store, menu)
    assert lunch.tick(WED.replace(day=13, hour=9, minute=31)) is None     # Tuesday
    assert lunch.tick(at("06:30")) is None and lunch.tick(at("15:00")) is None


def test_no_reminder_after_the_deadline(setup):
    store, menu, teams = setup
    lunch = L.Lunch(Graph(), teams, LLM(), store, menu)
    lunch.tick(at("09:30"))
    lunch.tick(at("10:46"))          # the 10:30 check was missed (server down, say)
    assert not any("reminder" in t for _, t in teams.sent)


def test_un_skipping_picks_up_where_it_was(setup):
    store, menu, teams = setup
    lunch = L.Lunch(Graph(), teams, LLM(), store, menu)
    lunch.tick(at("09:31"))
    L.skip_week(store, at("09:40"))
    assert lunch.tick(at("09:43"))["status"] == "skipped"
    L.order_anyway(store, at("09:50"))
    plan = lunch.tick(at("09:51"))
    assert plan["status"] == "collecting" and len(teams.sent) == 2      # nobody asked twice
