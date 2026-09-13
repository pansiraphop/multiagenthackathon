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


def _cartable(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (one row per product to search, rows that can't be searched).

    Two things happen here, both of them cost control. A blank ingredient name
    would search for nothing and add whatever Instacart shows first, so those
    rows are refused rather than guessed at. And the same ingredient can appear
    twice with different units (250 g butter for one recipe, 3 tbsp for
    another) — that is one product in a cart, not two, so it is searched once.
    """
    unique: dict[str, dict] = {}
    unusable: list[dict] = []
    for row in rows:
        name = (row.get("ingredient_name") or "").strip()
        if not name:
            unusable.append(row)
            continue
        unique.setdefault(name, row)
    return list(unique.values()), unusable


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
            try:
                page.goto(
                    f"{INSTACART_HOME}/store/cart",
                    wait_until="domcontentloaded",
                )
                cart_url = page.url
            except Exception as exc:
                # Items already added remain in the persistent cart even if the
                # remote session closes before the final navigation.
                print(f"      [warn] cart navigation failed: {exc}")
                cart_url = f"{INSTACART_HOME}/store/cart"
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
    week = week_start.isoformat()
    start, end = _delivery_window(week_start)
    if start is None or end is None:
        log_eval(
            STAGE,
            week,
            False,
            error_message="no feasible delivery window before earliest meal",
        )

    def mark(item: dict, status: str) -> None:
        # Stage 4 is delete-then-insert and may rerun while a long browser
        # session is active, replacing UUIDs. Update through its stable key,
        # and by name only: one cart product can cover two unit rows.
        db.update_where(
            "shopping_list",
            {"resolution_status": status},
            week_start_date=week,
            ingredient_name=item["ingredient_name"],
        )

    for item in result["added"]:
        mark(item, config.CART_READY_STATUS)
    failed_status = (
        "fallback_link" if result["method"] == "fallback_links" else "failed"
    )
    for item in result["failed"]:
        mark(item, failed_status)

    # Count from the table rather than from this run, so a resumed run reports
    # the whole week's cart instead of just the items it topped up.
    statuses = [
        row["resolution_status"]
        for row in db.select("shopping_list", "resolution_status", week_start_date=week)
    ]
    in_cart = sum(s == config.CART_READY_STATUS for s in statuses)
    # A resumed top-up that fails leaves a real cart plus a few link-only
    # items. Calling that "fallback_links" would understate the week.
    method = result["method"]
    cart_url = result["cart_url"]
    if method == "fallback_links" and in_cart:
        method = "mixed"
        # Stage 6 should link to the real cart, not to one search page.
        cart_url = f"{INSTACART_HOME}/store/cart"
    row = {
        "week_start_date": week,
        "cart_url": cart_url,
        "item_count": in_cart,
        "unresolved_item_count": len(statuses) - in_cart,
        "method": method,
        "delivery_window_start": start.isoformat() if start else None,
        "delivery_window_end": end.isoformat() if end else None,
    }
    db.delete_where("instacart_orders", week_start_date=week)
    return db.insert("instacart_orders", row)[0]


def run(
    week_start: date | None = None,
    *,
    dry_run: bool = False,
    fallback_only: bool = False,
    limit: int | None = None,
    force: bool = False,
) -> dict | None:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()
    rows = db.select("shopping_list", "*", week_start_date=week)
    items, unusable = _cartable(rows)
    if limit is not None:
        items = items[:max(0, limit)]

    print(f"[5/6] instacart     week of {week_start} | {len(rows)} rows -> "
          f"{len(items)} products to search")
    if not rows:
        print("      no shopping_list rows - run stage 4 first")
        if not dry_run:
            log_eval(STAGE, week, False, error_message="no shopping_list rows")
        return None
    for row in unusable:
        print(f"      [skip] row {row.get('id')} has no ingredient name")
        if not dry_run:
            log_eval(STAGE, str(row.get("id")), False,
                     error_message="shopping_list row has no ingredient_name")

    # One cart per week. Reels arriving all week accumulate into a single
    # order, so a second run must never re-add what is already in the cart.
    existing = db.select("instacart_orders", "*", week_start_date=week)
    order = existing[0] if existing else None
    if order and not force:
        if order.get("method") == "browser_automation":
            pending = [i for i in items
                       if i.get("resolution_status") != config.CART_READY_STATUS]
            if not pending:
                print(f"      already ordered this week: {order['item_count']} items "
                      f"in the cart - nothing to add (--force to rebuild)")
                if not dry_run:
                    log_eval(STAGE, f"{week}:already-ordered", True)
                return order
            # A previous run part-completed (expired session, flaky selector).
            # Top up only what is missing; the rest is already in the cart.
            print(f"      resuming this week's cart: {len(pending)} of {len(items)} "
                  f"not yet added")
            items = pending
        else:
            # A links-only row means nothing was ever added to a real cart, so
            # retrying the browser cannot duplicate anything.
            print("      previous run produced links only - retrying the browser cart")
    elif order and force:
        print("      --force: re-adding every item (may duplicate cart contents)")

    if dry_run:
        for item in items:
            print(f"      would add {item['ingredient_name']}: "
                  f"{search_url(item['ingredient_name'])}")
        print("      dry run - no browser session, no writes")
        return dict(_fallback_links(items), method="dry_run")

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
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if this week already has an order (may duplicate cart items)",
    )
    args = parser.parse_args()
    run(
        date.fromisoformat(args.week) if args.week else None,
        dry_run=args.dry_run,
        fallback_only=args.fallback_only,
        limit=args.limit,
        force=args.force,
    )


if __name__ == "__main__":
    main()
