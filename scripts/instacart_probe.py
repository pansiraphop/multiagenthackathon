"""Interactive Browserbase probe for Instacart login and selector discovery.

Run this before stage 5. Log in through the printed live-view URL, select a
store, then return here. The Browserbase context persists that login.
"""

from __future__ import annotations

import argparse
import sys
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browserbase import Browserbase
from playwright.sync_api import sync_playwright

import config


CANDIDATE_SELECTORS = (
    "[data-testid*='product']",
    "[data-testid*='item']",
    "button[aria-label*='Add']",
    "button:has-text('Add')",
    "[aria-label*='cart' i]",
    "[data-testid*='cart']",
)


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("BROWSERBASE_API_KEY", config.BROWSERBASE_API_KEY),
            ("BROWSERBASE_PROJECT_ID", config.BROWSERBASE_PROJECT_ID),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)} in .env")


def _context_id(bb: Browserbase) -> str:
    if config.BROWSERBASE_CONTEXT_ID:
        return config.BROWSERBASE_CONTEXT_ID
    context = bb.contexts.create(
        project_id=config.BROWSERBASE_PROJECT_ID,
        name="instacook-instacart",
    )
    return context.id


def run(ingredient: str, wait_seconds: int | None = None) -> None:
    _require_config()
    bb = Browserbase(api_key=config.BROWSERBASE_API_KEY)
    context_id = _context_id(bb)
    session = bb.sessions.create(
        project_id=config.BROWSERBASE_PROJECT_ID,
        api_timeout=config.BROWSER_SESSION_TIMEOUT_SECONDS,
        browser_settings={"context": {"id": context_id, "persist": True}},
    )
    live = bb.sessions.debug(session.id)

    print(f"\nBrowserbase context: {context_id}")
    print(f"Live view: {live.debugger_fullscreen_url}")
    if not config.BROWSERBASE_CONTEXT_ID:
        print(
            "\nAfter this probe closes, add this to .env:\n"
            f'BROWSERBASE_CONTEXT_ID="{context_id}"'
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.connect_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(config.BROWSER_SELECTOR_TIMEOUT_MS)
        page.set_default_navigation_timeout(config.BROWSER_NAV_TIMEOUT_MS)
        page.goto("https://www.instacart.com/", wait_until="domcontentloaded")

        if wait_seconds is None:
            input(
                "\nOpen the live view, log in, set your ZIP/store, add a payment "
                "method if you want Place order to work, dismiss banners, "
                "then press Enter here..."
            )
        else:
            print(
                "\nOpen the live view and log in now. The probe will continue "
                f"automatically in {wait_seconds} seconds."
            )
            threading.Event().wait(wait_seconds)

        logged_out = False
        for selector in (
            "a:has-text('Log in')",
            "button:has-text('Log in')",
            "a:has-text('Sign in')",
            "button:has-text('Sign in')",
        ):
            try:
                if page.locator(selector).first.is_visible(timeout=800):
                    logged_out = True
                    break
            except Exception:
                continue
        if logged_out:
            print(
                "\nWARNING: still seeing Log in — stage 5 will refuse to order. "
                "Finish login in the live view and re-run the probe."
            )
        else:
            print("\nLogin looks present (no Log in button).")

        query = urllib.parse.quote(ingredient, safe="")
        search = (
            f"https://www.instacart.com/store/{config.INSTACART_RETAILER}/search/{query}"
            if config.INSTACART_RETAILER
            else f"https://www.instacart.com/store/search/{query}"
        )
        page.goto(search, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        print(f"\nSelector candidates for {page.url}:")
        for selector in CANDIDATE_SELECTORS:
            locator = page.locator(selector)
            try:
                count = locator.count()
                sample = ""
                if count:
                    first = locator.first
                    sample = (
                        first.get_attribute("data-testid")
                        or first.get_attribute("aria-label")
                        or first.inner_text(timeout=1000)
                    )
                print(f"  {selector!r}: {count}  sample={sample!r}")
            except Exception as exc:  # selector diagnostics must continue
                print(f"  {selector!r}: ERROR {exc}")

        # Checkout surface smoke check — does not click Place order.
        try:
            page.goto(
                "https://www.instacart.com/store/cart",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(1500)
            for selector in (
                "button:has-text('Go to checkout')",
                "button:has-text('Checkout')",
                "button:has-text('Place order')",
            ):
                try:
                    visible = page.locator(selector).first.is_visible(timeout=800)
                except Exception:
                    visible = False
                print(f"  checkout {selector!r}: {'visible' if visible else 'missing'}")
        except Exception as exc:
            print(f"  checkout smoke check skipped: {exc}")

        output = Path(config.FAILURE_SHOT_DIR) / "instacart_probe.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=True)
        print(f"\nSaved screenshot: {output}")
        browser.close()

    print(
        "\nThe context syncs when the session closes. Wait a few seconds, then "
        "run the probe again to confirm the login survives."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ingredient", nargs="?", default="tomato")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        help="wait this long for live-view login instead of prompting",
    )
    args = parser.parse_args()
    try:
        run(args.ingredient, wait_seconds=args.wait_seconds)
    except Exception as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
