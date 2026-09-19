"""
SPIKE PROBE 1 (Playwright 1.5x) — real accessibility snapshot + targeting.
Run locally:  python3 probe_browser.py

Validates: (1) usable a11y tree as YAML for LLM perception,
           (2) rung-1 role+name targeting with no test IDs,
           (3) coordinates-inline via boxes=True (our rung-4, captured for free),
           (4) generic on-screen detection of a business outcome.
"""
import pathlib
from playwright.sync_api import sync_playwright

PAGE = pathlib.Path(__file__).parent / "members.html"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(f"file://{PAGE}")

    # (a) a11y tree as YAML — candidate LLM perception format
    print("=== ARIA SNAPSHOT (YAML) ===")
    print(page.locator("body").aria_snapshot())

    # (a2) same snapshot WITH bounding boxes inline — rung-1 + rung-4 in one call
    print("\n=== ARIA SNAPSHOT WITH BOXES (role+name+coords together) ===")
    print(page.locator("body").aria_snapshot(boxes=True))

    # (b) rung-1 targeting: find controls by ROLE+NAME only, no test IDs
    print("\n=== RUNG-1 TARGETING (role+name) ===")
    field = page.get_by_role("textbox", name="Member ID")
    button = page.get_by_role("button", name="Search")
    print("found textbox 'Member ID':", field.count() == 1)
    print("found button 'Search':   ", button.count() == 1)

    # (c) happy path -> read output
    field.fill("12345")
    button.click()
    print("read Savings Balance:    ", page.get_by_label("Savings Balance").inner_text())

    # (d) not-found path -> detect generically from on-screen text
    field.fill("99999")
    button.click()
    print("not-found alert text:    ", repr(page.get_by_role("alert").inner_text()))

    browser.close()
    print("\nPROBE 1 OK")
