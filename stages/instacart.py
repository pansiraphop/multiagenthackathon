"""Stage 5: add missing ingredients to Instacart through Browserbase."""

from __future__ import annotations

import argparse
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any

import config
from lib import browser as browser_lib
from lib import db
from lib.evals import log_eval
from lib.external import call_external_api

STAGE = "instacart"
INSTACART_HOME = "https://www.instacart.com"

# Confirmed against the live public DOM on 2026-09-13. Instacart currently puts
# the product name in the Add button's aria-label; there is no product-card
# data-testid wrapper around that button.
ADD_BUTTON_SELECTORS = (
    "button[aria-label^='Add ']",
    "button[aria-label='Add']",
    "button:has-text('Add')",
)
CART_STATE_SELECTORS = (
    "[data-testid*='cart']",
    "a[aria-label*='cart' i]",
    "button[aria-label*='cart' i]",
)


def search_url(ingredient_name: str) -> str:
    query = urllib.parse.quote(ingredient_name.strip(), safe="")
    if config.INSTACART_RETAILER:
        retailer = urllib.parse.quote(config.INSTACART_RETAILER.strip(), safe="")
        return f"{INSTACART_HOME}/store/{retailer}/search/{query}"
    return f"{INSTACART_HOME}/store/search/{query}"


def _first_match(page: Any, selectors: tuple[str, ...]) -> Any:
    locator = page.locator(", ".join(selectors)).first
    locator.wait_for(state="visible", timeout=config.BROWSER_SELECTOR_TIMEOUT_MS)
    return locator


def _cart_state(page: Any) -> str | None:
    for selector in CART_STATE_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count():
                node = locator.first
                return (
                    node.get_attribute("aria-label")
                    or node.inner_text(timeout=500)
                    or node.get_attribute("data-testid")
                )
        except Exception:
            continue
    return None


def search_and_add_item(page: Any, item: dict) -> None:
    """Search one ingredient, add the first result, and verify UI state changed."""
    page.goto(search_url(item["ingredient_name"]), wait_until="domcontentloaded")
    browser_lib.dismiss_overlays(page)

    before = _cart_state(page)
    add = _first_match(page, ADD_BUTTON_SELECTORS)
    add.click()

    def added() -> bool:
        after = _cart_state(page)
        if before is not None and after is not None and after != before:
            return True
        # Instacart commonly replaces Add with quantity controls after success.
        try:
            return add.count() == 0 or not add.is_visible()
        except Exception:
            return True

    page.wait_for_timeout(500)
    if not added():
        raise RuntimeError(
            f"Add click was not reflected in cart for {item['ingredient_name']}"
        )


def _browser_cart(items: list[dict]) -> dict:
    added: list[dict] = []
    failed: list[dict] = []

    with browser_lib.session() as page:
        for item in items:
            error: Exception | None = None
            retries_used = 0

            def on_fail(exc: Exception, attempt: int, final: bool) -> None:
                nonlocal error, retries_used
                error = exc
                retries_used = min(attempt + 1, config.BROWSER_ITEM_RETRIES)
                if final:
                    browser_lib.screenshot(page, str(item.get("id", "unknown")))

            _, ok = browser_lib.with_retry(
                lambda: search_and_add_item(page, item),
                attempts=config.BROWSER_ITEM_RETRIES + 1,
                on_fail=on_fail,
            )
            if ok:
                added.append(item)
                log_eval(
                    STAGE,
                    str(item.get("id", item["ingredient_name"])),
                    True,
                    retry_count=retries_used,
                )
                print(f"      added {item['ingredient_name']}")
            else:
                failed.append(item)
                log_eval(
                    STAGE,
                    str(item.get("id", item["ingredient_name"])),
                    False,
                    retry_count=retries_used,
                    error_message=str(error) if error else "unknown browser failure",
                )
                print(f"      [skip] {item['ingredient_name']}: {error}")

        if added:
            page.goto(f"{INSTACART_HOME}/store/cart", wait_until="domcontentloaded")
            cart_url = page.url
        else:
            cart_url = None

    return {
        "cart_url": cart_url,
        "links": [],
        "added": added,
        "failed": failed,
        "method": "browser_automation",
    }


def _fallback_links(items: list[dict]) -> dict:
    links = [search_url(item["ingredient_name"]) for item in items]
    return {
        "cart_url": links[0] if links else INSTACART_HOME,
        "links": links,
        "added": [],
        "failed": list(items),
        "method": "fallback_links",
    }


def _delivery_window(
    week_start: date,
    *,
    plans: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    plans = plans if plans is not None else db.select(
        "meal_plan", "planned_start_time", week_start_date=week_start.isoformat()
    )
    starts = [
        datetime.fromisoformat(row["planned_start_time"]).astimezone(config.TIMEZONE)
        for row in plans
        if row.get("planned_start_time")
    ]
    if not starts:
        return None, None

    now = (now or config.now_local()).astimezone(config.TIMEZONE)
    delivery_end = min(starts) - timedelta(hours=config.DELIVERY_BUFFER_HRS)
    earliest_start = now + timedelta(hours=config.DELIVERY_LEAD_HOURS)
    delivery_start = max(earliest_start, delivery_end - timedelta(hours=1))
    if delivery_start >= delivery_end:
        return None, None
    return delivery_start, delivery_end


def _write_order(week_start: date, result: dict) -> dict:
    start, end = _delivery_window(week_start)
    if start is None or end is None:
        log_eval(
            STAGE,
            week_start.isoformat(),
            False,
            error_message="no feasible delivery window before earliest meal",
        )
    row = {
        "week_start_date": week_start.isoformat(),
        "cart_url": result["cart_url"],
        "item_count": len(result["added"]),
        "unresolved_item_count": len(result["failed"]),
        "method": result["method"],
        "delivery_window_start": start.isoformat() if start else None,
        "delivery_window_end": end.isoformat() if end else None,
    }
    db.delete_where("instacart_orders", week_start_date=week_start.isoformat())
    return db.insert("instacart_orders", row)[0]


def run(
    week_start: date | None = None,
    *,
    dry_run: bool = False,
    fallback_only: bool = False,
    limit: int | None = None,
) -> dict | None:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()
    items = db.select("shopping_list", "*", week_start_date=week)
    if limit is not None:
        items = items[:max(0, limit)]

    print(f"[5/6] instacart     week of {week_start} | {len(items)} items")
    if not items:
        print("      no shopping_list rows - run stage 4 first")
        log_eval(STAGE, week, False, error_message="no shopping_list rows")
        return None

    if dry_run:
        for item in items:
            print(f"      would add {item['ingredient_name']}: {search_url(item['ingredient_name'])}")
        return _fallback_links(items)

    if fallback_only:
        result = _fallback_links(items)
        log_eval(STAGE, week, True)
    else:
        result, ok = call_external_api(
            _browser_cart,
            items,
            stage=STAGE,
            input_ref=week,
        )
        if not ok or not result or not result["added"]:
            result = _fallback_links(items)
            print("      browser cart unavailable; using search-link fallback")

    written = _write_order(week_start, result)
    print(
        f"      wrote instacart_orders: method={written['method']}, "
        f"added={written['item_count']}, "
        f"unresolved={written['unresolved_item_count']}"
    )
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="print only; no browser or writes")
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="skip Browserbase and write search links",
    )
    parser.add_argument("--limit", type=int, help="only process the first N items")
    args = parser.parse_args()
    run(
        date.fromisoformat(args.week) if args.week else None,
        dry_run=args.dry_run,
        fallback_only=args.fallback_only,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
