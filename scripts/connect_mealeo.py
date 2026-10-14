"""One-time Mealeo sign-in for the assistant, done by a person.

Mealeo's login has Google's invisible reCAPTCHA, which refuses an automated browser
("Request is invalid [1]"). So a person logs in once, in a visible browser window,
and that browser profile keeps the session; the assistant's checkout then reuses the
profile headless. If Mealeo ever logs it out, run this again.

Usage:  python scripts/connect_mealeo.py
A browser window opens on Mealeo's login. Log in as the assistant (MEALEO_EMAIL);
the window closes by itself once you're in.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from agent.mealeo import SAATI
from agent.mealeo_order import PROFILE, logged_in


def main() -> int:
    load_dotenv(".env")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SAATI, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if logged_in(page):
            print("Already logged in to Mealeo.")
            ctx.close()
            return 0
        page.locator('a:has-text("Log In"), button:has-text("Log In")').first.click()
        page.wait_for_timeout(1500)
        if os.environ.get("MEALEO_EMAIL"):
            page.locator("#email").fill(os.environ["MEALEO_EMAIL"])
        print("Log in to Mealeo in the browser window (the email is filled in). Waiting up to 5 minutes...")
        deadline = time.time() + 300
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            try:
                if logged_in(page):
                    print("Logged in. The session is saved for the assistant.")
                    ctx.close()
                    return 0
            except Exception:  # noqa: BLE001 - the page may be navigating
                continue
        print("Timed out waiting for the login.")
        ctx.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
