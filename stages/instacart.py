"""Stage 5: add missing ingredients to Instacart through Browserbase.

When INSTACART_PLACE_ORDER is on (or --place-order), the same session continues
through checkout: pick Delivery, choose a window, and click Place order.
"""

from __future__ import annotations

import argparse
import re
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
LOGIN_SELECTORS = (
    "a:has-text('Log in')",
    "button:has-text('Log in')",
    "a:has-text('Sign in')",
    "button:has-text('Sign in')",
)
# Instacart's marketplace cart is a header drawer, not /store/cart (that 404s).
VIEW_CART_SELECTORS = (
    "button[aria-label*='View Cart' i]",
    "button[aria-label*='cart' i]",
)
ENTER_STORE_CART_SELECTORS = (
    "button:has-text('Continue Shopping')",
)
CHECKOUT_BUTTON_SELECTORS = (
    "button:has-text('Go to checkout')",
    "button:has-text('Checkout')",
    "a:has-text('Go to checkout')",
    "a:has-text('Checkout')",
    "[data-testid*='checkout' i]",
)
DELIVERY_TAB_SELECTORS = (
    "button:has-text('Delivery')",
    "[role='tab']:has-text('Delivery')",
    "a:has-text('Delivery')",
)
PLACE_ORDER_SELECTORS = (
    "button:has-text('Place order')",
    "button:has-text('Place Order')",
    "button[aria-label*='Place order' i]",
)
ORDER_CONFIRMED_SELECTORS = (
    "text=/order confirmed/i",
    "text=/thanks for your order/i",
    "text=/order placed/i",
    "text=/your order is confirmed/i",
    "[data-testid*='order-confirmation' i]",
)
PAYMENT_BLOCKER_SELECTORS = (
    "button:has-text('Add a payment method')",
    "button:has-text('Add payment')",
    "text=/add a payment method/i",
    "text=/card declined/i",
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


def _click_first(page: Any, selectors: tuple[str, ...], *, timeout_ms: int | None = None) -> bool:
    timeout = timeout_ms or config.BROWSER_SELECTOR_TIMEOUT_MS
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=timeout):
                locator.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _any_visible(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 1500) -> bool:
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            continue
    return False


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


def _cart_count(page: Any) -> int | None:
    """Parse the header badge ('Items in cart: 12'). None if unreadable."""
    state = _cart_state(page) or ""
    match = re.search(r"items?\s+in\s+cart:\s*(\d+)", state, re.I)
    if match:
        return int(match.group(1))
    # Fallback: trailing lone number in the cart control text.
    match = re.search(r"(\d+)\s*$", " ".join(state.split()))
    if match:
        return int(match.group(1))
    return None


def ensure_logged_in(page: Any) -> None:
    """Fail fast when the persistent Browserbase context lost its Instacart login."""
    if _any_visible(page, LOGIN_SELECTORS, timeout_ms=1200):
        raise RuntimeError(
            "Instacart session is logged out. Re-run "
            "`python scripts/instacart_probe.py tomato`, log in through the "
            "live view, then save BROWSERBASE_CONTEXT_ID."
        )


def search_and_add_item(page: Any, item: dict) -> None:
    """Search one ingredient, add the first result, and verify the cart grew."""
    page.goto(search_url(item["ingredient_name"]), wait_until="domcontentloaded")
    browser_lib.dismiss_overlays(page)
    ensure_logged_in(page)

    before_count = _cart_count(page)
    before_state = _cart_state(page)
    add = _first_match(page, ADD_BUTTON_SELECTORS)
    add.click()
    page.wait_for_timeout(900)

    # Only trust a real cart-badge increase. Button disappearance alone is a
    # false positive (modals, navigation, stale locators) and was marking the
    # DB as added_to_cart while Instacart stayed empty.
    after_count = _cart_count(page)
    after_state = _cart_state(page)
    if before_count is not None and after_count is not None:
        if after_count > before_count:
            return
        raise RuntimeError(
            f"Add click did not grow the cart for {item['ingredient_name']} "
            f"(was {before_count}, now {after_count})"
        )
    if before_state and after_state and after_state != before_state:
        return
    raise RuntimeError(
        f"Add click was not reflected in cart for {item['ingredient_name']}"
    )


def _slot_label_matches(label: str, start: datetime, end: datetime) -> bool:
    """Best-effort match of an Instacart slot label against our delivery window."""
    text = " ".join(label.lower().split())
    if not text:
        return False
    # Prefer labels that mention a time in our window (e.g. "3:00pm – 4:00pm").
    times = re.findall(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text)
    if not times:
        # "Arrives between 3pm-5pm" style sometimes omits minutes on one side.
        return False

    def to_minutes(hour: str, minute: str | None, meridiem: str) -> int:
        h = int(hour) % 12
        if meridiem == "pm":
            h += 12
        return h * 60 + int(minute or 0)

    slot_minutes = [to_minutes(h, m, ampm) for h, m, ampm in times]
    window_start = start.hour * 60 + start.minute
    window_end = end.hour * 60 + end.minute
    return any(window_start - 30 <= minute <= window_end + 30 for minute in slot_minutes)


def _select_delivery_window(
    page: Any,
    *,
    delivery_start: datetime | None,
    delivery_end: datetime | None,
) -> None:
    """Pick Delivery and a time slot overlapping the planned window when possible."""
    _click_first(page, DELIVERY_TAB_SELECTORS, timeout_ms=2500)
    page.wait_for_timeout(800)

    if delivery_start is None or delivery_end is None:
        return

    slot_selectors = (
        "button[aria-label*='delivery' i]",
        "button[aria-label*='arrives' i]",
        "[data-testid*='time-slot' i] button",
        "[data-testid*='timeslot' i]",
        "button:has-text('am')",
        "button:has-text('pm')",
    )
    for selector in slot_selectors:
        try:
            buttons = page.locator(selector)
            count = min(buttons.count(), 24)
            preferred = None
            for i in range(count):
                button = buttons.nth(i)
                if not button.is_visible(timeout=300):
                    continue
                label = (
                    button.get_attribute("aria-label")
                    or button.inner_text(timeout=500)
                    or ""
                )
                if _slot_label_matches(label, delivery_start, delivery_end):
                    preferred = button
                    break
                if preferred is None:
                    preferred = button
            if preferred is not None:
                preferred.click(timeout=config.BROWSER_SELECTOR_TIMEOUT_MS)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _order_confirmed(page: Any) -> bool:
    url = (page.url or "").lower()
    if any(token in url for token in ("/orders/", "/store/orders", "confirmation", "thank")):
        return True
    return _any_visible(page, ORDER_CONFIRMED_SELECTORS, timeout_ms=2500)


def storefront_url() -> str:
    if config.INSTACART_RETAILER:
        retailer = urllib.parse.quote(config.INSTACART_RETAILER.strip(), safe="")
        return f"{INSTACART_HOME}/store/{retailer}/storefront"
    return INSTACART_HOME


def open_cart_drawer(page: Any) -> None:
    """Open the header cart drawer (marketplace no longer has /store/cart)."""
    if not _click_first(page, VIEW_CART_SELECTORS, timeout_ms=5000):
        raise RuntimeError("Could not find the View Cart button")
    page.wait_for_timeout(1200)
    # Multi-retailer accounts land on a Carts list first; enter the configured store.
    if config.INSTACART_RETAILER:
        retailer = config.INSTACART_RETAILER.strip()
        try:
            named = page.locator(
                f"button:has-text('{retailer}'), [aria-label*='{retailer}' i]"
            ).first
            if named.is_visible(timeout=800):
                named.click(timeout=config.BROWSER_SELECTOR_TIMEOUT_MS)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass
    if _click_first(page, ENTER_STORE_CART_SELECTORS, timeout_ms=1500):
        page.wait_for_timeout(2000)
        # Inside a storefront, open that store's cart drawer again.
        _click_first(page, VIEW_CART_SELECTORS, timeout_ms=4000)
        page.wait_for_timeout(1200)


def place_order(
    page: Any,
    *,
    delivery_start: datetime | None = None,
    delivery_end: datetime | None = None,
) -> str:
    """Drive cart drawer → checkout_v4 → Place order. Returns confirmation URL."""
    page.goto(storefront_url(), wait_until="domcontentloaded")
    browser_lib.dismiss_overlays(page)
    ensure_logged_in(page)
    open_cart_drawer(page)

    if not _click_first(page, CHECKOUT_BUTTON_SELECTORS,
                        timeout_ms=config.BROWSER_CHECKOUT_TIMEOUT_MS):
        browser_lib.screenshot(page, "checkout-no-button")
        raise RuntimeError("Could not find a Checkout button in the cart drawer")

    page.wait_for_timeout(2500)
    browser_lib.dismiss_overlays(page)
    _select_delivery_window(
        page, delivery_start=delivery_start, delivery_end=delivery_end
    )

    if _any_visible(page, PAYMENT_BLOCKER_SELECTORS, timeout_ms=2000):
        browser_lib.screenshot(page, "checkout-payment-blocker")
        raise RuntimeError(
            "Checkout reached payment but no card is on the Instacart account. "
            "Open the Browserbase live view, click Add a payment method, save a "
            "card, then re-run with --place-order."
        )

    if not _click_first(page, PLACE_ORDER_SELECTORS,
                        timeout_ms=config.BROWSER_CHECKOUT_TIMEOUT_MS):
        # checkout_v4 shows Continue until the order can be submitted.
        if not _click_first(page, ("button:has-text('Continue')",),
                            timeout_ms=5000):
            browser_lib.screenshot(page, "checkout-no-place-order")
            raise RuntimeError(
                "Could not find Place order — add a payment method on the "
                "Instacart account in the Browserbase live view, then retry."
            )
        page.wait_for_timeout(2000)
        if _any_visible(page, PAYMENT_BLOCKER_SELECTORS, timeout_ms=1500):
            browser_lib.screenshot(page, "checkout-payment-blocker")
            raise RuntimeError(
                "Checkout blocked on payment. Add a card to the Instacart "
                "account used in Browserbase, then re-run with --place-order."
            )
        if not _click_first(page, PLACE_ORDER_SELECTORS,
                            timeout_ms=config.BROWSER_CHECKOUT_TIMEOUT_MS):
            browser_lib.screenshot(page, "checkout-no-place-order")
            raise RuntimeError("Continue did not reveal a Place order button")

    page.wait_for_timeout(2500)
    deadline = datetime.now().timestamp() + (config.BROWSER_CHECKOUT_TIMEOUT_MS / 1000)
    while datetime.now().timestamp() < deadline:
        if _order_confirmed(page):
            return page.url
        page.wait_for_timeout(1000)

    if _any_visible(page, PAYMENT_BLOCKER_SELECTORS, timeout_ms=800):
        browser_lib.screenshot(page, "checkout-payment-blocker")
        raise RuntimeError(
            "Checkout blocked on payment. Add a card to the Instacart account "
            "used in Browserbase, then re-run with --place-order."
        )

    browser_lib.screenshot(page, "checkout-unconfirmed")
    raise RuntimeError(
        f"Place order clicked but confirmation was not detected (url={page.url})"
    )


def _browser_cart(
    items: list[dict],
    *,
    place_order_flag: bool = False,
    delivery_start: datetime | None = None,
    delivery_end: datetime | None = None,
) -> dict:
    added: list[dict] = []
    failed: list[dict] = []
    order_url: str | None = None
    checkout_error: str | None = None

    with browser_lib.session() as page:
        page.goto(INSTACART_HOME, wait_until="domcontentloaded")
        browser_lib.dismiss_overlays(page)
        ensure_logged_in(page)

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
                # Persist immediately so a killed session still resumes cleanly.
                week_key = item.get("week_start_date") or config.week_start().isoformat()
                if item.get("ingredient_name"):
                    db.update_where(
                        "shopping_list",
                        {"resolution_status": config.CART_READY_STATUS},
                        week_start_date=week_key,
                        ingredient_name=item["ingredient_name"],
                    )
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
                page.goto(storefront_url(), wait_until="domcontentloaded")
                open_cart_drawer(page)
                cart_url = page.url or storefront_url()
            except Exception as exc:
                # Items already added remain in the persistent cart even if the
                # remote session closes before the final navigation.
                print(f"      [warn] cart navigation failed: {exc}")
                cart_url = storefront_url()
        else:
            cart_url = None

        if place_order_flag and added:
            try:
                order_url = place_order(
                    page,
                    delivery_start=delivery_start,
                    delivery_end=delivery_end,
                )
                cart_url = order_url
                print(f"      placed order: {order_url}")
                log_eval(STAGE, "checkout", True)
            except Exception as exc:
                checkout_error = str(exc)
                browser_lib.screenshot(page, "checkout-failed")
                print(f"      [warn] checkout failed: {exc}")
                log_eval(STAGE, "checkout", False, error_message=checkout_error)

    method = "browser_automation"
    if order_url:
        method = "browser_ordered"
    elif place_order_flag and added and checkout_error:
        method = "browser_automation"  # cart stands; order did not

    return {
        "cart_url": cart_url,
        "links": [],
        "added": added,
        "failed": failed,
        "method": method,
        "order_placed": bool(order_url),
        "checkout_error": checkout_error,
    }


def _fallback_links(items: list[dict]) -> dict:
    links = [search_url(item["ingredient_name"]) for item in items]
    # App CTA should land on the store, not a single search for the first item.
    return {
        "cart_url": storefront_url() if items else INSTACART_HOME,
        "links": links,
        "added": [],
        "failed": list(items),
        "method": "fallback_links",
        "order_placed": False,
        "checkout_error": None,
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

    ready_status = (
        config.ORDERED_STATUS if result.get("order_placed") else config.CART_READY_STATUS
    )
    for item in result["added"]:
        mark(item, ready_status)
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
    in_cart = sum(
        s in (config.CART_READY_STATUS, config.ORDERED_STATUS) for s in statuses
    )
    # A resumed top-up that fails leaves a real cart plus a few link-only
    # items. Calling that "fallback_links" would understate the week.
    method = result["method"]
    cart_url = result["cart_url"]
    if method == "fallback_links" and in_cart:
        method = "mixed"
        # Stage 6 should link to the real cart, not to one search page.
        cart_url = storefront_url()
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


def _already_ordered(order: dict | None) -> bool:
    return bool(order and order.get("method") == "browser_ordered")


def run(
    week_start: date | None = None,
    *,
    dry_run: bool = False,
    fallback_only: bool = False,
    limit: int | None = None,
    force: bool = False,
    place_order_flag: bool | None = None,
) -> dict | None:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()
    place_order_flag = (
        config.INSTACART_PLACE_ORDER if place_order_flag is None else place_order_flag
    )
    rows = db.select("shopping_list", "*", week_start_date=week)
    items, unusable = _cartable(rows)
    if limit is not None:
        items = items[:max(0, limit)]

    print(f"[5/6] instacart     week of {week_start} | {len(rows)} rows -> "
          f"{len(items)} products to search"
          f"{' | will place order' if place_order_flag else ''}")
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
    if _already_ordered(order) and not force:
        print(f"      already placed this week's Instacart order "
              f"({order['item_count']} items) - nothing to do (--force to rebuild)")
        if not dry_run:
            log_eval(STAGE, f"{week}:already-ordered", True)
        return order

    if order and not force:
        pending = [
            i for i in items
            if i.get("resolution_status")
            not in (config.CART_READY_STATUS, config.ORDERED_STATUS)
        ]
        if order.get("method") in config.BROWSER_CART_METHODS:
            if not pending and not place_order_flag:
                print(f"      already ordered this week: {order['item_count']} items "
                      f"in the cart - nothing to add (--force to rebuild)")
                if not dry_run:
                    log_eval(STAGE, f"{week}:already-ordered", True)
                return order
            if not pending and place_order_flag:
                # Cart is full; this run only has to pull checkout through.
                print("      cart already built - proceeding to Place order")
                items = []
            else:
                # A previous run part-completed (expired session, flaky selector).
                # Top up only what is missing; the rest is already in the cart.
                print(f"      resuming this week's cart: {len(pending)} of "
                      f"{len(items)} not yet added")
                items = pending
        else:
            # Links-only (or mixed) rows: still skip anything already marked
            # added_to_cart so a mid-run resume does not duplicate.
            if not pending:
                print(f"      shopping list already fully carted "
                      f"({len(items)} items) - nothing to add")
                if not dry_run:
                    log_eval(STAGE, f"{week}:already-ordered", True)
                return order
            print(f"      previous run was links-only / incomplete - "
                  f"Browserbase topping up {len(pending)} pending of {len(items)}")
            items = pending
    elif order and force:
        print("      --force: re-adding every item (may duplicate cart contents)")

    if dry_run:
        for item in items:
            print(f"      would add {item['ingredient_name']}: "
                  f"{search_url(item['ingredient_name'])}")
        if place_order_flag:
            print("      would also Place order after the cart is filled")
        print("      dry run - no browser session, no writes")
        return dict(_fallback_links(items), method="dry_run")

    delivery_start, delivery_end = _delivery_window(week_start)

    if fallback_only:
        result = _fallback_links(items)
        log_eval(STAGE, week, True)
    elif not items and place_order_flag:
        # Checkout-only resume: open the existing cart and place the order.
        def checkout_only() -> dict:
            with browser_lib.session() as page:
                page.goto(INSTACART_HOME, wait_until="domcontentloaded")
                browser_lib.dismiss_overlays(page)
                ensure_logged_in(page)
                order_url = place_order(
                    page,
                    delivery_start=delivery_start,
                    delivery_end=delivery_end,
                )
                print(f"      placed order: {order_url}")
                return {
                    "cart_url": order_url,
                    "links": [],
                    "added": [
                        i for i in _cartable(rows)[0]
                        if i.get("resolution_status")
                        in (config.CART_READY_STATUS, config.ORDERED_STATUS)
                    ],
                    "failed": [],
                    "method": "browser_ordered",
                    "order_placed": True,
                    "checkout_error": None,
                }

        result, ok = call_external_api(
            checkout_only,
            stage=STAGE,
            input_ref=f"{week}:checkout",
        )
        if not ok or not result:
            print("      checkout unavailable; leaving the existing cart as-is")
            return order
    else:
        result, ok = call_external_api(
            _browser_cart,
            items,
            place_order_flag=place_order_flag,
            delivery_start=delivery_start,
            delivery_end=delivery_end,
            stage=STAGE,
            input_ref=week,
        )
        if not ok or not result or (items and not result["added"]):
            result = _fallback_links(items)
            print("      browser cart unavailable; using search-link fallback")

    written = _write_order(week_start, result)
    print(
        f"      wrote instacart_orders: method={written['method']}, "
        f"added={written['item_count']}, "
        f"unresolved={written['unresolved_item_count']}"
        f"{', ORDER PLACED' if result.get('order_placed') else ''}"
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
    parser.add_argument(
        "--place-order",
        action="store_true",
        default=None,
        help="after filling the cart, continue through Instacart checkout",
    )
    parser.add_argument(
        "--no-place-order",
        action="store_true",
        help="fill the cart only, even if INSTACART_PLACE_ORDER=true",
    )
    args = parser.parse_args()
    place: bool | None
    if args.no_place_order:
        place = False
    elif args.place_order:
        place = True
    else:
        place = None
    run(
        date.fromisoformat(args.week) if args.week else None,
        dry_run=args.dry_run,
        fallback_only=args.fallback_only,
        limit=args.limit,
        force=args.force,
        place_order_flag=place,
    )


if __name__ == "__main__":
    main()
