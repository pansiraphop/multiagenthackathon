"""The Instagram DM agent — the whole system, driven by conversation.

    python -m stages.concierge --say "what am I cooking tuesday"
    python -m stages.concierge --chat          # interactive, same code path
    python -m stages.concierge --history       # what it remembers

Send it a reel and ingestion takes it (stage 1, already wired). Send it text and
this runs: the model picks tools, the tools are the pipeline stages, the reply
goes back as a DM.

Why this is a thin layer rather than a rewrite: every stage is already a plain
function reading and writing database rows. Nothing here reimplements planning
or shopping — the tools call the same `plan.run` and `shopping_list.run` the CLI
calls, so there is exactly one implementation of each behaviour and the agent
can never drift from it.

The loop is `client.beta.messages.tool_runner`, which handles the
call → execute → feed-back cycle. We supply the tools and the conversation.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

import anthropic
from anthropic import beta_tool

import config
from lib import db
from lib.evals import log_eval
from lib.llm import client as llm_client
from lib.normalize import normalize_ingredient, render_amount
from lib.schemas import advance_prep, attended_minutes
from stages import availability, plan, shopping_list
from stages.plan import usable_pantry

STAGE = "concierge"

# A DM is not a document. Long replies get truncated by Instagram and read as
# spam; markdown doesn't render at all.
SYSTEM = """You are InstaCook, reachable in the user's Instagram DMs.

You turn recipe reels they send you into an actual cooked week: you know what's
in their fridge, when they're free, and what they need to buy.

How to reply:
- Write like a person texting. One to three short sentences.
- No markdown, no bullet points, no headings — none of it renders in a DM.
- Lead with the answer. Don't narrate what you're about to do.
- Numbers and days are the useful part: "Tuesday 7:30pm, 25 minutes" beats
  "I've scheduled that for you."
- If a tool fails or there's nothing to report, say so plainly in one line.
- Never tell them a day or time until the scheduling tool has returned —
  those tools write Google Calendar before they finish, so a time you invent
  won't be on their calendar yet.

How to act:
- Use tools for anything factual. Never guess what's planned or in the pantry.
- When they mention food they have or bought, add it to the pantry without
  being asked to.
- They can direct you: "make the katsu on Thursday", "move the wings to
  Saturday", "drop the ragu". Use schedule_recipe, move_meal and remove_meal
  for those rather than re-planning the whole week around one request.
- plan_week is for "sort out my week". It replaces everything, so ask first if
  a plan already exists.
- plan_week, schedule_recipe, move_meal and remove_meal already sync Google
  Calendar and rebuild the shopping list. Don't call sync_calendar or
  build_shopping_list after them unless a tool result said that step failed.
- Don't ask permission for reversible things.
- Plenty of messages aren't requests at all. Answer cooking questions, explain
  a step, suggest a substitution — you don't need a tool to be useful.
- When they say \"this\" / \"this reel\" / \"I want to cook this\", call
  get_recipes — a reel they just sent may already be pending or extracted.
  Never claim nothing came through without checking.
"""


# ---------------------------------------------------------------------------
# tools — thin wrappers over the stages, never reimplementations
# ---------------------------------------------------------------------------

@beta_tool
def get_pantry() -> str:
    """List what's currently in the pantry, with how soon each item expires."""
    today = config.now_local().date()
    rows = sorted(db.select("pantry"),
                  key=lambda r: (r["expiry_date"] or "9999-99-99"))
    if not rows:
        return "The pantry is empty."

    lines = []
    for row in rows:
        qty = row.get("quantity")
        amount = f"{float(qty):g} {row.get('unit') or ''}".strip() if qty else ""
        expiry = row.get("expiry_date")
        if expiry:
            days = (date.fromisoformat(expiry) - today).days
            when = "expired" if days < 0 else f"{days}d left"
        else:
            when = "staple"
        lines.append(f"{row['ingredient_name']} {amount} ({when})")
    return f"{len(rows)} items: " + "; ".join(lines)


@beta_tool
def add_pantry_items(items: str) -> str:
    """Add or update pantry items.

    Args:
        items: JSON list of objects with "name" and optional "quantity",
            "unit", "expiry_date" (YYYY-MM-DD). Example:
            [{"name": "chicken thighs", "quantity": 500, "unit": "g",
              "expiry_date": "2026-09-16"}]
    """
    try:
        parsed = json.loads(items)
    except json.JSONDecodeError as exc:
        return f"Could not read that list: {exc}"
    if isinstance(parsed, dict):
        parsed = [parsed]

    added = []
    for item in parsed:
        name, qty, unit = normalize_ingredient(
            item.get("name"), item.get("quantity"), item.get("unit"))
        if not name:
            continue
        row = {
            "ingredient_name": name,
            "quantity": qty if qty is not None else 1,
            "unit": unit,
            "expiry_date": item.get("expiry_date") or None,
        }
        existing = db.select("pantry", "*", ingredient_name=name)
        if existing:
            db.update("pantry", existing[0]["id"], row)
        else:
            db.insert("pantry", row)
        added.append(name)

    return f"Added to the pantry: {', '.join(added)}." if added else "Nothing to add."


@beta_tool
def remove_pantry_item(name: str) -> str:
    """Remove one item from the pantry, for example when it's been used up."""
    clean, _qty, _unit = normalize_ingredient(name)
    rows = db.select("pantry", "*", ingredient_name=clean)
    if not rows:
        return f"{clean} isn't in the pantry."
    db.delete_where("pantry", id=rows[0]["id"])
    return f"Removed {clean}."


@beta_tool
def get_recipes() -> str:
    """List recipes from reels — extracted ones plus any still being processed."""
    ready = db.successful_recipes()
    pending = db.pending_recipes()
    if not ready and not pending:
        return "No recipes yet. Send me a reel."

    parts: list[str] = []
    if ready:
        parts.append(
            f"{len(ready)} ready: "
            + "; ".join(
                f"{r['title']} ({attended_minutes(r)} min, "
                f"{r.get('cuisine') or 'other'})"
                for r in ready
            )
        )
    if pending:
        parts.append(
            f"{len(pending)} still extracting: "
            + "; ".join(
                (r.get("raw_caption") or r.get("source_url") or "reel")
                .strip()
                .splitlines()[0][:60]
                for r in pending
            )
        )
    return " | ".join(parts)


@beta_tool
def get_week_plan() -> str:
    """What's currently planned for the week, with days, times and reasons."""
    week = config.week_start().isoformat()
    meals = sorted(db.select("meal_plan", "*", week_start_date=week),
                   key=lambda m: m["planned_start_time"])
    if not meals:
        return "Nothing is planned for this week yet."

    recipes = {r["id"]: r for r in db.successful_recipes()}
    lines = []
    for meal in meals:
        recipe = recipes.get(meal["recipe_id"]) or {}
        start = datetime.fromisoformat(
            meal["planned_start_time"]).astimezone(config.TIMEZONE)
        lead = advance_prep(recipe)
        note = f", needs {lead // 60}h prep ahead" if lead else ""
        lines.append(
            f"{start:%A} {start:%H:%M} - {recipe.get('title')} "
            f"({attended_minutes(recipe)} min{note}); "
            f"reason: {meal.get('score_reason') or 'n/a'}")
    return " | ".join(lines)


@beta_tool
def plan_week(replace_existing: bool = False) -> str:
    """Plan the week: find free evenings, fit recipes, then write Google Calendar.

    Args:
        replace_existing: Must be true to overwrite a plan that already exists.
    """
    week_start = config.week_start()
    week = week_start.isoformat()

    existing = db.select("meal_plan", "*", week_start_date=week)
    if existing and not replace_existing:
        return (f"There's already a plan with {len(existing)} meals. "
                f"Call again with replace_existing=true to redo it.")

    for meal in existing:
        _drop_calendar_event(meal)

    slots = availability.run(week_start=week_start)
    if not slots:
        return "Couldn't read any free cook windows from the calendar."

    meals = plan.run(week_start=week_start, use_llm=True)
    if not meals:
        return "Nothing fit the windows available this week."
    cal = _push_calendar(week_start)
    shop = _refresh_shopping(week_start)
    return f"Planned {len(meals)} meals.{cal}{shop} " + get_week_plan.func()

@beta_tool
def get_shopping_list() -> str:
    """What needs buying this week, after subtracting the pantry."""
    week = config.week_start().isoformat()
    rows = db.select("shopping_list", "*", week_start_date=week)
    if not rows:
        return "No shopping list built yet for this week."

    order = db.select("instacart_orders", "*", week_start_date=week)
    cart = f" Cart: {order[0]['cart_url']}" if order and order[0].get("cart_url") else ""
    items = ", ".join(
        f"{r['ingredient_name']} "
        f"{render_amount({'quantity': float(r['quantity_needed']), 'unit': r['unit']})}"
        for r in rows[:40])
    return f"{len(rows)} items to buy: {items}.{cart}"


@beta_tool
def build_shopping_list() -> str:
    """Work out what to buy for the planned week and link it on Instacart."""
    week_start = config.week_start()
    msg = _refresh_shopping(week_start).strip()
    if msg.lower().startswith("nothing to buy"):
        return msg
    if msg.lower().startswith("shopping list update failed"):
        return msg
    pantry = len(usable_pantry())
    return f"{msg} {pantry} pantry items were taken into account."


@beta_tool
def whats_for_dinner(day: str = "") -> str:
    """What's planned for a particular day.

    Args:
        day: A weekday name like "tuesday", or "today"/"tomorrow". Empty means
            the next upcoming meal.
    """
    week = config.week_start().isoformat()
    meals = sorted(db.select("meal_plan", "*", week_start_date=week),
                   key=lambda m: m["planned_start_time"])
    if not meals:
        return "Nothing is planned for this week yet."

    recipes = {r["id"]: r for r in db.successful_recipes()}
    now = config.now_local()
    wanted = day.strip().lower()

    if wanted in ("", "next"):
        upcoming = [m for m in meals
                    if datetime.fromisoformat(m["planned_start_time"]) >= now]
        chosen = upcoming[0] if upcoming else meals[-1]
    else:
        if wanted == "today":
            wanted = f"{now:%A}".lower()
        elif wanted == "tomorrow":
            from datetime import timedelta
            wanted = f"{now + timedelta(days=1):%A}".lower()
        matches = [
            m for m in meals
            if f"{datetime.fromisoformat(m['planned_start_time']).astimezone(config.TIMEZONE):%A}".lower()
            == wanted
        ]
        if not matches:
            return f"Nothing planned for {day}."
        chosen = matches[0]

    recipe = recipes.get(chosen["recipe_id"]) or {}
    start = datetime.fromisoformat(
        chosen["planned_start_time"]).astimezone(config.TIMEZONE)
    ingredients = ", ".join(i["name"] for i in recipe.get("ingredients", [])[:8])
    return (f"{start:%A} {start:%H:%M}: {recipe.get('title')} "
            f"({attended_minutes(recipe)} min, serves {recipe.get('servings')}). "
            f"Ingredients: {ingredients}. "
            f"Recipe: {config.APP_BASE_URL}/cook/{chosen['id']}")


# ---------------------------------------------------------------------------
# direct control — "make the katsu on Thursday"
#
# plan_week is the automatic path. These are the manual one: the user names a
# reel and a day and it happens. Everything still goes through the same fit
# rules the planner uses, so a manual choice can't produce a week the automatic
# planner would have rejected.
# ---------------------------------------------------------------------------

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")


def _find_recipe(query: str) -> dict | None:
    """Match a recipe by whatever the user actually typed.

    "the katsu", "katsu curry" and "Chicken Katsu Curry" all have to land on
    the same row — nobody retypes a title exactly.
    """
    recipes = db.successful_recipes()
    wanted = (query or "").strip().lower()
    if not wanted:
        return None

    for recipe in recipes:
        if (recipe.get("title") or "").lower() == wanted:
            return recipe
    for recipe in recipes:
        if wanted in (recipe.get("title") or "").lower():
            return recipe

    # Word overlap, so "katsu curry" still finds "Chicken Katsu Curry".
    words = {w for w in wanted.split() if len(w) > 2}
    best, best_score = None, 0
    for recipe in recipes:
        title_words = set((recipe.get("title") or "").lower().split())
        score = len(words & title_words)
        if score > best_score:
            best, best_score = recipe, score
    return best


def _slots_on(day_name: str, week_start: date) -> list[dict]:
    """Cook windows on a named weekday, earliest first."""
    wanted = (day_name or "").strip().lower()
    rows = db.select("cook_slots", "*", week_start_date=week_start.isoformat())
    out = []
    for slot in rows:
        start = datetime.fromisoformat(slot["slot_start"]).astimezone(config.TIMEZONE)
        if f"{start:%A}".lower() == wanted:
            out.append(slot)
    return sorted(out, key=lambda s: s["slot_start"])


def _fits(recipe: dict, slot: dict, now: datetime) -> str | None:
    """None if it fits, otherwise the reason it doesn't, in plain words."""
    need = attended_minutes(recipe) + config.SLOT_BUFFER_MINUTES
    if need > slot["duration_minutes"]:
        return (f"needs {attended_minutes(recipe)} min but that window is only "
                f"{slot['duration_minutes']} min")
    lead = advance_prep(recipe)
    start = datetime.fromisoformat(slot["slot_start"])
    if lead and now + timedelta(minutes=lead) > start:
        return (f"needs {lead // 60}h of prep beforehand and there isn't that "
                f"much time before then")
    return None


def _minutes_of(slot: dict) -> int:
    start = datetime.fromisoformat(slot["slot_start"]).astimezone(config.TIMEZONE)
    return start.hour * 60 + start.minute


def _drop_calendar_event(meal: dict) -> None:
    """Remove one meal's Google Calendar event, if we created one."""
    event_id = meal.get("calendar_event_id")
    if not event_id:
        return
    try:
        from lib.external import call_external_api
        from lib.google_auth import calendar_service

        service = calendar_service()
        call_external_api(
            lambda eid: service.events().delete(
                calendarId=config.GOOGLE_CALENDAR_ID, eventId=eid).execute(),
            event_id, stage=STAGE, input_ref=meal.get("id") or "")
    except Exception:
        # A stale event id must not block editing the plan.
        pass


def _push_calendar(week_start: date) -> str:
    """Write the current plan to Google Calendar before we tell the user."""
    from stages import calendar_sync

    try:
        created = calendar_sync.run(week_start=week_start)
    except Exception as exc:
        return f" Calendar sync failed ({exc}) — the plan is saved but not on GCal yet."
    if created:
        n = len(created)
        return f" On your calendar now ({n} new event{'s' if n != 1 else ''})."
    return " On your calendar."


def _refresh_shopping(week_start: date) -> str:
    """Rebuild the shopping list and leave an Instacart link the app can open."""
    from stages import instacart

    try:
        rows = shopping_list.run(week_start=week_start)
    except Exception as exc:
        return f" Shopping list update failed ({exc})."
    if not rows:
        week = week_start.isoformat()
        db.delete_where("instacart_orders", week_start_date=week)
        return " Nothing to buy — pantry covers it, or the plan is empty."

    # Fast path for DMs: always write a cart_url so the app CTA works now.
    # A full Browserbase fill is slow; run that separately / via CLI when needed.
    # Never place an order from this path.
    try:
        order = instacart.run(
            week_start=week_start, fallback_only=True, place_order_flag=False)
    except Exception as exc:
        return (f" Shopping list updated ({len(rows)} items) but Instacart "
                f"link failed ({exc}).")

    url = (order or {}).get("cart_url") or "Instacart"
    return (f" Shopping list updated ({len(rows)} items). "
            f"Open them on Instacart from the app ({url}).")


def _place(recipe: dict, slot: dict, week_start: date, reason: str) -> dict:
    """Write one meal into a slot, clearing whatever occupied either end."""
    week = week_start.isoformat()
    start = datetime.fromisoformat(slot["slot_start"])
    minutes = attended_minutes(recipe)

    for existing in db.select("meal_plan", "*", week_start_date=week):
        same_slot = existing.get("cook_slot_id") == slot["id"]
        same_recipe = existing["recipe_id"] == recipe["id"]
        if same_slot or same_recipe:
            _drop_calendar_event(existing)
            db.delete_where("meal_plan", id=existing["id"])
            if existing.get("cook_slot_id"):
                db.update("cook_slots", existing["cook_slot_id"],
                          {"assigned": False})

    row = db.insert("meal_plan", {
        "recipe_id": recipe["id"],
        "cook_slot_id": slot["id"],
        "week_start_date": week,
        "planned_date": start.astimezone(config.TIMEZONE).date().isoformat(),
        "planned_start_time": slot["slot_start"],
        "planned_end_time": (start + timedelta(minutes=minutes)).isoformat(),
        "score_reason": reason,
        "status": "planned",
    })[0]
    db.update("cook_slots", slot["id"], {"assigned": True})
    return row


@beta_tool
def get_free_windows() -> str:
    """The evenings the user is free to cook this week, read from their calendar."""
    week_start = config.week_start()
    slots = db.select("cook_slots", "*", week_start_date=week_start.isoformat())
    if not slots:
        slots = availability.run(week_start=week_start)
    if not slots:
        return "Couldn't read any free windows from the calendar."

    lines = []
    for slot in sorted(slots, key=lambda s: s["slot_start"]):
        start = datetime.fromisoformat(
            slot["slot_start"]).astimezone(config.TIMEZONE)
        taken = " (taken)" if slot.get("assigned") else ""
        lines.append(f"{start:%A} {start:%H:%M}, {slot['duration_minutes']} min{taken}")
    return "Free windows: " + "; ".join(lines)


@beta_tool
def schedule_recipe(recipe: str, day: str, time: str = "") -> str:
    """Put one recipe on one day and write it to Google Calendar before returning.

    Args:
        recipe: The dish, however the user said it — "the katsu", "katsu curry".
        day: A weekday name like "thursday".
        time: Optional "HH:MM", to choose between windows on a busy evening.
    """
    week_start = config.week_start()
    found = _find_recipe(recipe)
    if not found:
        return f"I don't have a recipe matching '{recipe}'. Send me the reel?"
    if day.strip().lower() not in WEEKDAYS:
        return f"'{day}' isn't a weekday I recognise."

    slots = _slots_on(day, week_start)
    if not slots:
        return (f"There's no free cooking window on {day.title()} — the "
                f"calendar is full that evening.")

    if time.strip() and ":" in time:
        try:
            hh, mm = (int(part) for part in time.split(":")[:2])
            slots.sort(key=lambda s: abs(_minutes_of(s) - (hh * 60 + mm)))
        except ValueError:
            pass

    now = config.now_local()
    problems = []
    for slot in slots:
        why = _fits(found, slot, now)
        if why:
            problems.append(why)
            continue
        row = _place(found, slot, week_start,
                     f"You asked for this on {day.title()}.")
        start = datetime.fromisoformat(
            row["planned_start_time"]).astimezone(config.TIMEZONE)
        cal = _push_calendar(week_start)
        shop = _refresh_shopping(week_start)
        return (f"{found['title']} is on for {start:%A} at {start:%H:%M}, "
                f"{attended_minutes(found)} min.{cal}{shop}")

    return (f"{found['title']} won't fit {day.title()}: {problems[0]}. "
            f"Want me to find a day that works?")


@beta_tool
def move_meal(recipe: str, to_day: str, time: str = "") -> str:
    """Move an already-planned meal to a different day.

    Args:
        recipe: The dish to move.
        to_day: The weekday to move it to.
        time: Optional "HH:MM" preference.
    """
    found = _find_recipe(recipe)
    if not found:
        return f"I don't have a recipe matching '{recipe}'."

    week = config.week_start().isoformat()
    planned = [m for m in db.select("meal_plan", "*", week_start_date=week)
               if m["recipe_id"] == found["id"]]
    if not planned:
        return f"{found['title']} isn't on the plan, so there's nothing to move."
    return schedule_recipe.func(recipe=recipe, day=to_day, time=time)


@beta_tool
def remove_meal(recipe: str) -> str:
    """Take one meal off the week's plan, free that evening, and update Google Calendar."""
    found = _find_recipe(recipe)
    if not found:
        return f"I don't have a recipe matching '{recipe}'."

    week_start = config.week_start()
    week = week_start.isoformat()
    removed = 0
    for meal in db.select("meal_plan", "*", week_start_date=week):
        if meal["recipe_id"] != found["id"]:
            continue
        _drop_calendar_event(meal)
        if meal.get("cook_slot_id"):
            db.update("cook_slots", meal["cook_slot_id"], {"assigned": False})
        db.delete_where("meal_plan", id=meal["id"])
        removed += 1

    if not removed:
        return f"{found['title']} wasn't on the plan."
    cal = _push_calendar(week_start)
    shop = _refresh_shopping(week_start)
    return (f"Dropped {found['title']} — that evening is free again."
            f"{cal}{shop}")


@beta_tool
def sync_calendar() -> str:
    """Re-write the planned week into Google Calendar.

    Prefer this only when a previous sync failed, or after shopping adds a cart
    link that should appear on the events. Scheduling tools already sync.
    """
    return _push_calendar(config.week_start()).strip()


TOOLS = [
    # pantry
    get_pantry, add_pantry_items, remove_pantry_item,
    # what's available and what's planned
    get_recipes, get_free_windows, get_week_plan, whats_for_dinner,
    # changing the plan — automatic, then by hand
    plan_week, schedule_recipe, move_meal, remove_meal,
    # downstream
    get_shopping_list, build_shopping_list, sync_calendar,
]


# ---------------------------------------------------------------------------
# conversation
# ---------------------------------------------------------------------------

HISTORY_TURNS = 12


def history(sender_id: str, limit: int = HISTORY_TURNS) -> list[dict]:
    """Recent turns, oldest first — enough to be conversational, not a transcript."""
    rows = (
        db.client().table("conversations").select("*")
        .eq("sender_id", sender_id)
        .order("created_at", desc=True).limit(limit)
        .execute().data or []
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def remember(sender_id: str, role: str, content: str) -> None:
    db.insert("conversations",
              {"sender_id": sender_id, "role": role, "content": content})


def respond(sender_id: str, message: str) -> str:
    """Run one turn: history + message -> tools -> reply. Never raises."""
    import time
    started = time.time()

    messages = history(sender_id) + [{"role": "user", "content": message}]
    remember(sender_id, "user", message)

    try:
        runner = llm_client().beta.messages.tool_runner(
            model=config.MODEL,
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        final = runner.until_done()
        reply = "".join(
            block.text for block in final.content
            if getattr(block, "type", None) == "text").strip()
    except Exception as exc:                                   # noqa: BLE001
        log_eval(STAGE, sender_id, False,
                 duration_ms=int((time.time() - started) * 1000),
                 error_message=str(exc))
        # A DM agent that goes silent looks broken. Say something true instead.
        return "Something went wrong on my end - try that again in a moment."

    if not reply:
        reply = "Done."
    remember(sender_id, "assistant", reply)
    log_eval(STAGE, sender_id, True,
             duration_ms=int((time.time() - started) * 1000))
    return reply


def handle_dm(sender_id: str, message: str) -> str:
    """Entry point for the webhook: answer, and send it back as a DM."""
    reply = respond(sender_id, message)
    try:
        from lib import instagram
        if instagram.configured():
            instagram.send_dm(sender_id, reply)
    except Exception as exc:                                   # noqa: BLE001
        print(f"  [warn] could not send DM to {sender_id}: {exc}")
    return reply


# ---------------------------------------------------------------------------
# CLI — the same code path the webhook uses, so testing is honest
# ---------------------------------------------------------------------------

def _cli_sender() -> str:
    return db.latest_sender_id() or "cli-tester"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--say", help="send one message and print the reply")
    parser.add_argument("--chat", action="store_true", help="interactive session")
    parser.add_argument("--history", action="store_true", help="show what it remembers")
    parser.add_argument("--forget", action="store_true", help="clear the conversation")
    parser.add_argument("--sender", help="override the sender id")
    args = parser.parse_args()

    sender = args.sender or _cli_sender()

    if args.forget:
        db.delete_where("conversations", sender_id=sender)
        print(f"cleared the conversation with {sender}")
        return

    if args.history:
        for turn in history(sender, limit=40):
            who = "you" if turn["role"] == "user" else "instacook"
            print(f"{who:>10}: {turn['content']}")
        return

    if args.say:
        print(respond(sender, args.say))
        return

    if args.chat:
        print(f"InstaCook - talking as {sender}. Ctrl-C to stop.\n")
        while True:
            try:
                message = input("you: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if message:
                print(f"\ninstacook: {respond(sender, message)}\n")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
