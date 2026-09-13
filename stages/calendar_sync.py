"""Stage 6 — write the week into Google Calendar.

One event per planned meal at its real cook time, plus one for the grocery
delivery window. This is the stage the demo actually shows: everything before
it is invisible until the events appear in a real calendar.

    python -m stages.calendar_sync --dry-run   # print the events, write nothing
    python -m stages.calendar_sync             # create them
    python -m stages.calendar_sync --clear     # remove the ones we created

Reads meal_plan + recipes + recipe_ingredients + instacart_orders (optional).
Writes calendar_event_id back onto each meal_plan row.

Deliberately tolerant of stage 5 being unfinished: with no instacart_orders
row the meal events are still created, they just say the grocery list is
pending instead of carrying a cart link. Nothing here places an order — this
stage only ever READS what stage 5 wrote.

Idempotent: a meal that already has a calendar_event_id is skipped, so
re-running never duplicates events. That matters because demos get re-run.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import config
from lib import db
from lib.evals import log_eval
from lib.external import call_external_api
from lib.normalize import render_amount
from lib.schemas import advance_prep, attended_minutes

STAGE = "calendar"

# Every created event carries this so --clear can find ours without touching
# the user's real entries, and so stage 2 can exclude them from busy time.
MARKER = config.CALENDAR_MARKER


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------

def _ingredient_lines(recipe: dict) -> str:
    lines = []
    for ing in recipe.get("ingredients", []):
        row = {
            "quantity": float(ing["quantity"]) if ing["quantity"] is not None else None,
            "unit": ing.get("unit"),
            "qualitative_note": ing.get("qualitative_note"),
            "is_approximate": ing.get("is_approximate"),
        }
        lines.append(f"- {ing['name']} - {render_amount(row)}")
    return "\n".join(lines) if lines else "- (none recorded)"


def _step_lines(recipe: dict) -> str:
    steps = recipe.get("steps") or []
    if not steps:
        return "(no method recorded)"
    return "\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1))


def build_description(meal: dict, recipe: dict, order: dict | None) -> str:
    """The body of the calendar invite. This is what the cook actually reads."""
    parts: list[str] = []

    if meal.get("score_reason"):
        parts.append(meal["score_reason"])

    active = recipe.get("est_time_minutes")
    attended = attended_minutes(recipe)
    timing = f"{attended} min total"
    if active and active != attended:
        timing = f"{active} min hands-on, {timing}"
    facts = [timing]
    if recipe.get("cuisine"):
        facts.append(str(recipe["cuisine"]))
    if recipe.get("servings"):
        facts.append(f"serves {recipe['servings']}")
    parts.append(" | ".join(facts))

    # Be visibly honest when the reel didn't actually contain a recipe.
    if recipe.get("provenance") == "reconstructed":
        parts.append(
            "NOTE: this reel didn't include a full recipe, so this was "
            "reconstructed from the dish name and web research. Check it before "
            "you shop."
        )

    lead = advance_prep(recipe)
    if lead:
        start_by = datetime.fromisoformat(
            meal["planned_start_time"]) - timedelta(minutes=lead)
        start_by = start_by.astimezone(config.TIMEZONE)
        hours = lead // 60
        parts.append(
            f"START AHEAD: needs {hours}h of marinating/chilling first - "
            f"begin by {start_by:%a %d %b, %H:%M}."
        )

    parts.append("INGREDIENTS\n" + _ingredient_lines(recipe))
    parts.append("STEPS\n" + _step_lines(recipe))

    if order and order.get("cart_url"):
        unresolved = order.get("unresolved_item_count") or 0
        if order.get("method") == "fallback_links":
            # The zero-dependency floor: a search link, not a built cart.
            # Calling it "your cart" would be a lie the cook discovers in the shop.
            parts.append(
                f"Groceries: no cart could be built automatically, so this is a "
                f"search link and you'll add items yourself ({unresolved} to "
                f"find).\n{order['cart_url']}"
            )
        else:
            note = (f" ({unresolved} item(s) need picking by hand)"
                    if unresolved else "")
            parts.append(f"Groceries: {order['cart_url']}{note}")
    else:
        parts.append("Groceries: shopping list not built yet.")

    if recipe.get("source_url"):
        parts.append(f"Reel: {recipe['source_url']}")

    # Shipped from the very first version so stage 7 needs no rework. Until
    # that page exists this serves a plain recipe view; it is never a dead link.
    parts.append(f"Cook along: {config.APP_BASE_URL}/cook/{meal['id']}")

    parts.append(MARKER)
    return "\n\n".join(parts)


def build_event(meal: dict, recipe: dict, order: dict | None) -> dict:
    """A Calendar API event body. Timezone is always explicit — see ground rule 2."""
    return {
        "summary": recipe.get("title") or "Cook something",
        "description": build_description(meal, recipe, order),
        "start": {"dateTime": meal["planned_start_time"],
                  "timeZone": str(config.TIMEZONE)},
        "end": {"dateTime": meal["planned_end_time"],
                "timeZone": str(config.TIMEZONE)},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 30}]},
    }


def build_delivery_event(order: dict, meals: list[dict]) -> dict | None:
    """One event over the delivery window, if stage 5 produced one."""
    start, end = order.get("delivery_window_start"), order.get("delivery_window_end")
    if not start or not end:
        return None

    # The fallback tier resolves nothing, so item_count is 0 while
    # unresolved_item_count holds the real total. "0 items arriving" would be
    # actively wrong on a calendar.
    count = order.get("item_count") or order.get("unresolved_item_count") or 0
    unblocks = ", ".join(
        sorted({m.get("_title", "") for m in meals if m.get("_title")})[:3])
    built = order.get("method") != "fallback_links"
    body = [
        f"{count} item(s) arriving." if built
        else f"{count} item(s) to buy - no cart was built, add them yourself.",
        f"Groceries: {order.get('cart_url') or 'no cart link'}",
    ]
    if unblocks:
        body.append(f"Needed for: {unblocks}")
    body.append(MARKER)

    return {
        "summary": f"Instacart delivery - {count} items",
        "description": "\n\n".join(body),
        "start": {"dateTime": start, "timeZone": str(config.TIMEZONE)},
        "end": {"dateTime": end, "timeZone": str(config.TIMEZONE)},
        "reminders": {"useDefault": False, "overrides": []},
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _load(week_start: date) -> tuple[list[dict], dict[str, dict], dict | None]:
    week = week_start.isoformat()
    meals = sorted(db.select("meal_plan", "*", week_start_date=week),
                   key=lambda m: m["planned_start_time"])
    recipes = {r["id"]: r for r in db.successful_recipes()}
    orders = db.select("instacart_orders", "*", week_start_date=week)
    return meals, recipes, (orders[0] if orders else None)


def clear(week_start: date) -> int:
    """Delete the events we created and reset the ids. Demo re-runs need this."""
    from lib.google_auth import calendar_service

    week = week_start.isoformat()
    service = calendar_service()
    removed = 0

    for meal in db.select("meal_plan", "*", week_start_date=week):
        if not meal.get("calendar_event_id"):
            continue
        _res, ok = call_external_api(
            lambda eid: service.events().delete(
                calendarId=config.GOOGLE_CALENDAR_ID, eventId=eid).execute(),
            meal["calendar_event_id"], stage=STAGE, input_ref=meal["id"])
        # Reset regardless: if the event is already gone, the id is stale and
        # keeping it would make the row un-syncable forever.
        db.update("meal_plan", meal["id"],
                  {"calendar_event_id": None, "status": "planned"})
        removed += 1 if ok else 0

    for order in db.select("instacart_orders", "*", week_start_date=week):
        if not order.get("delivery_event_id"):
            continue
        call_external_api(
            lambda eid: service.events().delete(
                calendarId=config.GOOGLE_CALENDAR_ID, eventId=eid).execute(),
            order["delivery_event_id"], stage=STAGE, input_ref=order["id"])
        db.update("instacart_orders", order["id"], {"delivery_event_id": None})
        removed += 1

    print(f"[6/6] calendar      removed {removed} event(s) for the week of {week_start}")
    return removed


def run(week_start: date | None = None, dry_run: bool = False,
        force: bool = False) -> list[dict]:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()

    meals, recipes, order = _load(week_start)

    if not meals:
        print("[6/6] calendar      no meal_plan for this week - run the planner first")
        log_eval(STAGE, week, False, error_message="no meal_plan rows")
        return []

    cart = "cart linked" if (order and order.get("cart_url")) else "no cart yet"
    mode = " (dry run)" if dry_run else ""
    print(f"[6/6] calendar      {len(meals)} meals, {cart}{mode}")

    service = None
    if not dry_run:
        from lib.google_auth import calendar_service
        service = calendar_service()

    created: list[dict] = []
    for meal in meals:
        recipe = recipes.get(meal["recipe_id"])
        if not recipe:
            print(f"      [skip] meal {meal['id'][:8]} has no successful recipe")
            continue

        start = datetime.fromisoformat(
            meal["planned_start_time"]).astimezone(config.TIMEZONE)
        title = (recipe.get("title") or "?")[:30]

        if meal.get("calendar_event_id") and not force:
            print(f"      {start:%a %d %H:%M}  {title:30s} already scheduled")
            continue

        body = build_event(meal, recipe, order)

        if dry_run:
            print(f"      {start:%a %d %H:%M}  {title:30s} would create")
            print("      " + "-" * 62)
            for line in body["description"].splitlines():
                print(f"      | {line}")
            print("      " + "-" * 62)
            continue

        event, ok = call_external_api(
            lambda b: service.events().insert(
                calendarId=config.GOOGLE_CALENDAR_ID, body=b).execute(),
            body, stage=STAGE, input_ref=meal["id"])

        if not ok or not event:
            db.update("meal_plan", meal["id"], {"status": "failed"})
            print(f"      {start:%a %d %H:%M}  {title:30s} FAILED")
            continue

        db.update("meal_plan", meal["id"],
                  {"calendar_event_id": event["id"], "status": "scheduled"})
        created.append(event)
        print(f"      {start:%a %d %H:%M}  {title:30s} created")

    # Delivery window, if stage 5 has run and produced one.
    for meal in meals:
        meal["_title"] = (recipes.get(meal["recipe_id"]) or {}).get("title")

    if order and not (order.get("delivery_event_id") and not force):
        delivery = build_delivery_event(order, meals)
        if delivery is None:
            print("      [note] no delivery window on the order - skipping that event")
        elif dry_run:
            print(f"      delivery window would be blocked out")
        else:
            event, ok = call_external_api(
                lambda b: service.events().insert(
                    calendarId=config.GOOGLE_CALENDAR_ID, body=b).execute(),
                delivery, stage=STAGE, input_ref=order["id"])
            if ok and event:
                db.update("instacart_orders", order["id"],
                          {"delivery_event_id": event["id"]})
                created.append(event)
                print(f"      delivery window blocked out")
    elif not order:
        print("      [note] no instacart_orders row yet - meals scheduled without "
              "a cart link")

    if not dry_run:
        log_eval(STAGE, week, True)
        print(f"      created {len(created)} event(s)")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the events without touching Google")
    parser.add_argument("--clear", action="store_true",
                        help="delete the events we created and reset the ids")
    parser.add_argument("--force", action="store_true",
                        help="recreate even if already scheduled (use after --clear)")
    args = parser.parse_args()

    week = date.fromisoformat(args.week) if args.week else None
    if args.clear:
        clear(week or config.week_start())
    else:
        run(week_start=week, dry_run=args.dry_run, force=args.force)
