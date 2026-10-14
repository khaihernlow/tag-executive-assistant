"""Fill a Mealeo order for Saati Deli, up to the payment step, the way a person would.

Mealeo has no API, so this drives its website in a headless browser: each item
with its options into the bag, delivery date and window, the office address and
suite, the tip; then it reads Mealeo's own totals from checkout and stops.

It never pays and never submits. Paying by card needs the assistant's Mealeo login
(guests can't use a card), and pressing "Submit Delivery Order" is a separate step
to add once that login exists and Dave has said so. There is deliberately no code
here that touches payment or the submit button.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from agent.mealeo import SAATI

log = logging.getLogger("assistant.mealeo")

OFFICE = {"address": "12 Elmwood Road, Menands", "expect": "12 Elmwood Rd", "suite": "Suite 1"}
SHOTS = os.environ.get("MEALEO_SHOTS", "data/mealeo")   # a screenshot of each checkout, for Dave or debugging
# The browser profile holding the assistant's Mealeo session (scripts/connect_mealeo.py: a person logs
# in once, since the login's reCAPTCHA refuses automated browsers). Kept with the other secrets.
PROFILE = os.environ.get("MEALEO_PROFILE", ".secrets/mealeo-profile")


def logged_in(page: Any) -> bool:
    """Mealeo's header shows "Log In" (or "Log In or Sign Up") until someone is logged in."""
    header = page.locator('header a:visible, header button:visible, .navbar a:visible, a.btn:visible, button.btn:visible')
    return not any(t.strip().lower() in ("log in", "log in or sign up") for t in header.all_inner_texts())

# The same cleaning as the menu reader, in the page: "Add Add Swiss Cheese (+$1.25)" -> "Add Swiss Cheese".
_LABEL_JS = """(modal, name) => {
  const clean = (t) => t.replace(/\\s+/g, ' ').trim().replace(/\\s*\\(\\+?\\$[\\d.,]+\\)\\s*$/, '').replace(/^Add Add /, 'Add ');
  const label = [...modal.querySelectorAll('label')].find(l => clean(l.innerText).toLowerCase() === name.toLowerCase());
  if (!label) return false;
  label.scrollIntoView({block: 'center'});
  label.click();
  return true;
}"""


@dataclass
class Checkout:
    """What Mealeo's checkout says, once the bag is filled. Nothing is submitted."""
    items: list[dict[str, Any]] = field(default_factory=list)   # each as Mealeo lists it, with its price
    subtotal: float = 0.0
    delivery: float = 0.0
    tax: float = 0.0
    tip: float = 0.0
    total: float = 0.0
    when: str = ""
    address: str = ""
    logged_in: bool = False      # as the assistant (a guest can't pay by card)
    step: str = ""               # where it got to, so a failure says where
    problems: list[str] = field(default_factory=list)
    screenshot: str = ""


def _money(text: str) -> float:
    m = re.search(r"\$\s?([\d,]+\.\d\d)", text or "")
    return float(m.group(1).replace(",", "")) if m else 0.0


def fill_checkout(lines: list[dict[str, Any]], window: str = "11:45 AM", day: str = "Today",
                  tip_percent: int = 20, url: str = SAATI, headless: bool = True) -> Checkout:
    """Put these lines (each {"item", "options"}) in the bag for delivery to the office in the
    window starting `window` on `day` ("Today", "Tomorrow"), and read checkout's totals."""
    from playwright.sync_api import sync_playwright

    out = Checkout()
    with sync_playwright() as p:
        # The saved profile carries the assistant's Mealeo session; without it, a guest checkout.
        browser = (p.chromium.launch_persistent_context(PROFILE, headless=headless, viewport={"width": 1280, "height": 900})
                   if os.path.isdir(PROFILE) else p.chromium.launch(headless=headless).new_context(viewport={"width": 1280, "height": 900}))
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_default_timeout(15000)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if page.locator('button:has-text("Reject")').count():
                page.locator('button:has-text("Reject")').first.click()
            out.logged_in = logged_in(page)
            if not out.logged_in:
                out.problems.append("not logged in to Mealeo as the assistant (run scripts/connect_mealeo.py)")
            # A logged-in bag stays on the account between visits: start from an empty one, or
            # last time's items (a test, a week-old order) would be ordered again on top.
            out.step = "emptying the bag"
            if not _empty_bag(page):
                out.problems.append("couldn't empty the Mealeo bag first")
                return out
            # The delivery time and address are set straight after the first item goes in, every
            # time: Mealeo asks for them then, or (when the account remembers last time's, which may
            # be "Tomorrow") they're re-set from the bag's schedule link before anything else is added.
            scheduled = False
            for line in lines:
                out.step = f"adding {line['item']}"
                if not _add_item(page, line, out):
                    continue
                if not scheduled:
                    out.step = "setting the delivery time and address"
                    if not page.locator("#orderDate:visible").count():
                        page.locator("a.yellow-bg-link:visible").first.click()
                        page.locator("#orderDate").wait_for(state="visible")
                    if not _schedule(page, window, day, out):
                        return out
                    scheduled = True
            if not scheduled:
                out.problems.append("nothing could be added to the Mealeo bag")
                return out
            out.step = "going to checkout"
            go = page.locator('a:has-text("Checkout"), button:has-text("Checkout")').first
            go.click()
            page.wait_for_url("**/checkout**", timeout=30000)
            page.wait_for_timeout(3000)
            out.step = "suite and tip"
            _suite_and_tip(page, tip_percent, out)
            _read_checkout(page, out)
        except Exception as e:  # noqa: BLE001 - whatever the site did, Dave gets told rather than a crash
            out.problems.append(f"Mealeo, while {out.step or 'opening the page'}: {type(e).__name__}: {str(e).splitlines()[0][:160]}")
        finally:
            try:
                os.makedirs(SHOTS, exist_ok=True)
                out.screenshot = os.path.join(SHOTS, "checkout.png")
                page.screenshot(path=out.screenshot, full_page=page.url.endswith("/checkout"))
            except Exception:  # noqa: BLE001
                out.screenshot = ""
            browser.close()
    return out


def _empty_bag(page: Any) -> bool:
    for _ in range(40):
        remove = page.locator("a.removeItem:visible")
        if not remove.count():
            return True
        remove.first.click()
        page.wait_for_timeout(1500)
        confirm = page.locator('.modal:visible button:has-text("Remove"), .modal:visible button:has-text("Yes")')
        if confirm.count():
            confirm.first.click()
            page.wait_for_timeout(1500)
    return not page.locator("a.removeItem:visible").count()


def _add_item(page: Any, line: dict[str, Any], out: Checkout) -> bool:
    item = page.locator(f'a.menu-item:has(h4[title="{line["item"]}"])').first
    if not item.count():
        out.problems.append(f"{line['item']} isn't on the Mealeo page")
        return False
    item.scroll_into_view_if_needed()
    item.click()
    modal = page.locator("#mdl")
    modal.locator('button:has-text("Add to Order")').first.wait_for(state="visible")
    page.wait_for_timeout(800)
    for name in line.get("options") or []:
        if not modal.evaluate(_LABEL_JS, name):
            out.problems.append(f"{line['item']}: option \"{name}\" isn't there")
    button = modal.locator('button:has-text("Add to Order")').first
    price = _money(button.inner_text())
    button.click()
    page.wait_for_timeout(2500)
    if modal.is_visible() and modal.locator('button:has-text("Add to Order")').count():
        # Still open: Mealeo wants something (a required choice). Say what, close, move on.
        missing = modal.locator(".required-not-set, .stillReq h4").all_inner_texts()
        out.problems.append(f"{line['item']}: Mealeo wants a choice of {', '.join(m.strip() for m in missing) or 'something'}")
        modal.locator("button.close").first.click()
        return False
    out.items.append({"item": line["item"], "options": line.get("options") or [], "price": price})
    return True


def _schedule(page: Any, window: str, day: str, out: Checkout) -> bool:
    dates = page.locator("select#orderDate:visible").first.evaluate("e => [...e.options].map(o => o.text)")
    pick = next((d for d in dates if d.startswith(day) and "Closed" not in d), None)
    if not pick:
        out.problems.append(f"Saati Deli isn't taking orders for {day.lower()} (Mealeo shows: {', '.join(dates[1:4])})")
        return False
    page.locator("select#orderDate:visible").first.select_option(label=pick)
    page.wait_for_timeout(1500)
    windows = page.locator("select#orderTime:visible").first.evaluate("e => [...e.options].map(o => o.text)")
    slot = next((w for w in windows if w.startswith(window)), None)
    if not slot:
        out.problems.append(f"no {window} delivery window (Mealeo offers {', '.join(w for w in windows[1:] if 'AM' in w or 'Noon' in w)[:160]})")
        return False
    page.locator("select#orderTime:visible").first.select_option(label=slot)
    # Two schedule dialogs share these fields but not their ids (…-settings, …-info): use the visible one.
    address = page.locator(".modal:visible input[name=geoAddress]").first
    address.click()
    address.fill("")              # the update dialog comes pre-filled; typing would append to it
    page.wait_for_timeout(1000)
    address.type(OFFICE["address"], delay=100)
    try:
        page.locator(".pac-item").first.wait_for(state="visible", timeout=10000)
        page.locator(".pac-item").first.click()
    except Exception:  # noqa: BLE001 - Google's suggestions don't always render headless; the keyboard works
        address.press("ArrowDown")
        address.press("Enter")    # (in the update dialog this also submits it)
    page.wait_for_timeout(2500)
    dialog = page.locator(".modal:visible:has(input[name=geoAddress])")
    # Enter in the update dialog submits it: the button goes to its spinner and the dialog closes
    # a few seconds later. Give it that time before deciding the button still needs pressing.
    for _ in range(8):
        if not dialog.count() or dialog.locator("input[name=geoAddress]").first.input_value().strip() == "":
            break
        busy = page.evaluate("() => !![...document.querySelectorAll('.modal button.submit-btn')].find(b => b.offsetParent && !b.innerText.trim())")
        if not busy:
            break
        page.wait_for_timeout(1000)
    if dialog.count():
        typed = dialog.locator("input[name=geoAddress]").first.input_value()
        if OFFICE["expect"].lower() not in typed.lower():
            out.problems.append(f"the address came out as \"{typed}\"")
            return False
        alert = dialog.locator(".alert")
        if alert.count() and alert.first.inner_text().strip():
            out.problems.append(alert.first.inner_text().strip()[:200])
            return False
        # The dialog's one submit button: "Schedule Delivery" the first time, "Update Delivery Order"
        # when the account already had a schedule. Matching its label proved unreliable; whether it
        # worked is checked below from what the bag then shows (and at checkout, the address).
        submit = dialog.locator("button[type=submit]:visible")
        if not submit.count():
            out.problems.append("couldn't find the button to confirm the delivery time and address")
            return False
        submit.first.click()
        page.wait_for_timeout(3500)
        if page.locator(".modal:visible:has(input[name=geoAddress])").count():
            alert = page.locator(".modal:visible .alert")
            out.problems.append((alert.first.inner_text().strip() if alert.count() else "")[:200]
                                or "Mealeo didn't accept the delivery time and address")
            return False
    # However it was submitted, the bag now shows the schedule: it must be the day and window asked for.
    shown = " ".join(page.locator("a.yellow-bg-link:visible").all_inner_texts()).lower()
    start = window.lower().replace(" ", "")              # "11:45 AM" -> "11:45am"
    if day.lower() not in shown or start not in shown.replace(" ", ""):
        out.problems.append(f"Mealeo's delivery time shows \"{shown.strip()}\", not {day} {window}")
        return False
    out.when = slot
    return True


def _suite_and_tip(page: Any, tip_percent: int, out: Checkout) -> None:
    # The suite row opens a dialog holding the address AND the suite: fill only the suite
    # field, by its id (filling "the first text box" once typed the suite over the address).
    row = page.locator('text="Apt # / Suite / Room / Floor"').first
    if row.count():
        row.click()
        suite = page.locator("input#suite")
        try:
            suite.wait_for(state="visible", timeout=8000)
            suite.fill(OFFICE["suite"])
            page.locator('button:has-text("Update Delivery Order")').first.click()
            page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            out.problems.append("couldn't add the suite number")
    text = page.locator("body").inner_text()
    if OFFICE["suite"].lower() not in text.lower():
        out.problems.append(f"the delivery address doesn't show {OFFICE['suite']}")
    if OFFICE["expect"].lower() not in text.lower():
        out.problems.append(f"the delivery address isn't {OFFICE['expect']} any more")
    tip = page.locator(f'text="{tip_percent}%"').first
    if tip.count():
        tip.click()
        page.wait_for_timeout(1000)


def _read_checkout(page: Any, out: Checkout) -> None:
    text = page.locator("body").inner_text()

    def amount(label: str) -> float:
        m = re.search(rf"\n{re.escape(label)}\s*\n?\s*\$\s?([\d,]+\.\d\d)", "\n" + text)
        return float(m.group(1).replace(",", "")) if m else 0.0

    out.subtotal, out.delivery = amount("Subtotal"), amount("Delivery")
    out.tax, out.tip, out.total = amount("Tax & Service"), amount("Gratuity"), amount("Total")
    block = re.search(r"Delivery To\s*\n(.*?)\n\s*(?:Apt #|Hand Order|Leave)", text, re.S)
    out.address = ", ".join(line.strip() for line in block.group(1).splitlines() if line.strip()) if block else ""
    if not out.total:
        out.problems.append("couldn't read the total at checkout")
