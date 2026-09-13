"""Browserbase/Playwright reliability helpers for stage 5."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from browserbase import Browserbase
from playwright.sync_api import Page, sync_playwright

import config


@contextmanager
def session() -> Iterator[Page]:
    """Yield one page backed by the persistent Instacart Browserbase context."""
    missing = [
        name
        for name, value in (
            ("BROWSERBASE_API_KEY", config.BROWSERBASE_API_KEY),
            ("BROWSERBASE_PROJECT_ID", config.BROWSERBASE_PROJECT_ID),
            ("BROWSERBASE_CONTEXT_ID", config.BROWSERBASE_CONTEXT_ID),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}. Run scripts/instacart_probe.py first."
        )

    bb = Browserbase(api_key=config.BROWSERBASE_API_KEY)
    remote = bb.sessions.create(
        project_id=config.BROWSERBASE_PROJECT_ID,
        browser_settings={
            "context": {
                "id": config.BROWSERBASE_CONTEXT_ID,
                "persist": True,
            }
        },
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(remote.connect_url)
        try:
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(config.BROWSER_SELECTOR_TIMEOUT_MS)
            page.set_default_navigation_timeout(config.BROWSER_NAV_TIMEOUT_MS)
            yield page
        finally:
            browser.close()


def dismiss_overlays(page: Page) -> None:
    """Best-effort dismissal of common banners; absence is never an error."""
    selectors = (
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Got it')",
        "button:has-text('Not now')",
        "button[aria-label='Close']",
        "[data-testid='modal-close-button']",
    )
    for selector in selectors:
        try:
            candidate = page.locator(selector).first
            if candidate.is_visible(timeout=250):
                candidate.click(timeout=750)
        except Exception:  # overlays vary by region and session state
            continue


def screenshot(page: Page, name: str) -> Path | None:
    """Save a failure screenshot without allowing screenshot failure to escape."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)) or "unknown"
    output = Path(config.FAILURE_SHOT_DIR) / f"{safe_name}.png"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=True)
        return output
    except Exception as exc:  # diagnostics must not break the fallback path
        print(f"  [warn] failure screenshot failed ({name}): {exc}")
        return None


def with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int,
    on_fail: Callable[[Exception, int, bool], None] | None = None,
) -> tuple[Any, bool]:
    """Run ``fn`` up to ``attempts`` times and never raise.

    ``on_fail`` receives (exception, zero-based attempt index, is_final).
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return fn(), True
        except Exception as exc:  # browser failures are deliberately isolated
            final = attempt == attempts - 1
            if on_fail:
                on_fail(exc, attempt, final)
    return None, False
