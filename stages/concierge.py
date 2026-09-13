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
from datetime import date, datetime

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

How to act:
- Use tools for anything factual. Never guess what's planned or in the pantry.
- When they mention food they have or bought, add it to the pantry without
  being asked to.
- Planning the week needs cook windows first; get_week_plan and plan_week
  handle that for you.
- Don't ask permission for reversible things. Do ask before replacing a plan
  they already have.
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
    """List the recipes extracted from reels that are available to plan with."""
    rows = db.successful_recipes()
    if not rows:
        return "No recipes yet. Send me a reel."
    return f"{len(rows)} recipes: " + "; ".join(
        f"{r['title']} ({attended_minutes(r)} min, {r.get('cuisine') or 'other'})"
        for r in rows)


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
    """Plan the week: find free evenings, then fit recipes into them.

    Args:
        replace_existing: Must be true to overwrite a plan that already exists.
    """
    week_start = config.week_start()
    week = week_start.isoformat()

    existing = db.select("meal_plan", "*", week_start_date=week)
    if existing and not replace_existing:
        return (f"There's already a plan with {len(existing)} meals. "
                f"Call again with replace_existing=true to redo it.")

    slots = availability.run(week_start=week_start)
    if not slots:
        return "Couldn't read any free cook windows from the calendar."

    meals = plan.run(week_start=week_start, use_llm=True)
    if not meals:
        return "Nothing fit the windows available this week."
    return f"Planned {len(meals)} meals. " + get_week_plan()


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
    """Work out what to buy for the planned week, subtracting what's in the pantry."""
    week_start = config.week_start()
    rows = shopping_list.run(week_start=week_start)
    if not rows:
        return "Nothing to buy - either nothing is planned, or the pantry covers it."
    pantry = len(usable_pantry())
    return (f"{len(rows)} items to buy. {pantry} pantry items were taken into "
            f"account so they're not on the list.")


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


TOOLS = [
    get_pantry, add_pantry_items, remove_pantry_item,
    get_recipes, get_week_plan, plan_week,
    get_shopping_list, build_shopping_list, whats_for_dinner,
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
