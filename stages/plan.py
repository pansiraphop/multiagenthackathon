"""Stage 3 — pick meals and fit them into real cook windows.

Scoring and assignment are FULLY DETERMINISTIC. No LLM decides anything here.
That's on purpose: the plan has to be reproducible (it gets re-run on camera),
debuggable (read the score, don't re-prompt), and it keeps the eval numbers
about extraction quality rather than planner variance.

The one model call generates `score_reason` AFTER the decision is made — the
human-readable "why this meal, why now" for the calendar invite. It is batched
into a single request for the whole week and falls back to a template, so it
can never block or alter a plan.

    python -m stages.plan
    python -m stages.plan --show              # don't write
    python -m stages.plan --no-reasons        # skip the LLM call entirely
    python -m stages.plan --exclude-slot ID   # stage 5 uses this to re-plan

Reads recipes, recipe_ingredients, pantry, cook_slots. Writes meal_plan.
Idempotent: clears the week's plan and releases its slots first.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import config
from lib import db
from lib.evals import log_eval
from lib.llm import LLMFailure, call_llm_structured
from lib.normalize import is_non_food
from lib.prompts import SCORE_REASON_PROMPT
from lib.schemas import MealReasons, advance_prep, attended_minutes

STAGE = "planning"


# ---------------------------------------------------------------------------
# pantry
# ---------------------------------------------------------------------------

def usable_pantry(today: date | None = None) -> dict[str, dict]:
    """Pantry keyed by normalized name, excluding anything already expired.

    Expired stock is not urgent, it's gone. Counting it would both inflate the
    urgency score and leave the ingredient off the shopping list — the worst of
    both, since you'd plan a meal around food you have to throw away.
    """
    today = (today or config.now_local().date()).isoformat()
    pantry: dict[str, dict] = {}
    for row in db.select("pantry"):
        expiry = row.get("expiry_date")
        if expiry and expiry < today:
            continue
        pantry[row["ingredient_name"]] = row
    return pantry


def _shoppable(recipe: dict) -> list[dict]:
    """Ingredients that count for scoring. Water and ice aren't groceries."""
    return [i for i in recipe.get("ingredients", []) if not is_non_food(i["name"])]


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def expiry_urgency(recipe: dict, pantry: dict[str, dict],
                   today: date | None = None) -> float:
    """0..1 — how much cooking this rescues food that's about to go bad."""
    today = today or config.now_local().date()
    total = 0.0
    for ing in _shoppable(recipe):
        row = pantry.get(ing["name"])
        if not row or not row.get("expiry_date"):
            continue
        days = (date.fromisoformat(row["expiry_date"]) - today).days
        if days <= 1:
            total += 1.0
        elif days <= 7:
            total += (7 - days) / 6.0
    return min(1.0, total)


def pantry_overlap(recipe: dict, pantry: dict[str, dict]) -> float:
    """0..1 — fraction of the ingredients already on hand."""
    ingredients = _shoppable(recipe)
    if not ingredients:
        return 0.0
    have = sum(1 for i in ingredients if i["name"] in pantry)
    return have / len(ingredients)


def completeness(recipe: dict, pantry: dict[str, dict]) -> float:
    """0..1 — penalises recipes that imply a big shop. 8+ missing scores 0."""
    missing = sum(1 for i in _shoppable(recipe) if i["name"] not in pantry)
    return max(0.0, 1.0 - missing / 8.0)


def base_score(recipe: dict, pantry: dict[str, dict],
               today: date | None = None) -> float:
    return round(
        config.W_EXPIRY * expiry_urgency(recipe, pantry, today)
        + config.W_OVERLAP * pantry_overlap(recipe, pantry)
        + config.W_COMPLETENESS * completeness(recipe, pantry),
        4,
    )


def expiring_ingredients(recipe: dict, pantry: dict[str, dict],
                         today: date | None = None) -> list[tuple[str, int]]:
    """(name, days until expiry) for this recipe's soon-to-go items, soonest first."""
    today = today or config.now_local().date()
    found = []
    for ing in _shoppable(recipe):
        row = pantry.get(ing["name"])
        if not row or not row.get("expiry_date"):
            continue
        days = (date.fromisoformat(row["expiry_date"]) - today).days
        if days <= 7:
            found.append((ing["name"], days))
    return sorted(found, key=lambda pair: pair[1])


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------

def assign(
    recipes: list[dict],
    slots: list[dict],
    pantry: dict[str, dict],
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fit recipes into slots. Returns (assignments, unschedulable recipes).

    Walks slots in chronological order and takes the best-scoring recipe that
    actually fits. Because the walk is chronological, soonest-expiring
    ingredients naturally land on the earliest evenings — which is exactly the
    behaviour worth narrating in the demo.
    """
    now = now or config.now_local()
    scores = {r["id"]: base_score(r, pantry) for r in recipes}

    available = sorted(slots, key=lambda s: s["slot_start"])
    remaining = list(recipes)
    used_cuisines: set[str] = set()
    assignments: list[dict] = []

    for slot in available:
        if len(assignments) >= config.MEALS_PER_WEEK:
            break

        capacity = slot["duration_minutes"] - config.SLOT_BUFFER_MINUTES
        slot_start = datetime.fromisoformat(slot["slot_start"])

        candidates = []
        for recipe in remaining:
            if attended_minutes(recipe) > capacity:
                continue
            # Advance prep has to be startable before this slot. A dish needing
            # an overnight marinade can't go in tonight's window.
            lead = advance_prep(recipe)
            if lead and now + timedelta(minutes=lead) > slot_start:
                continue
            candidates.append(recipe)

        if not candidates:
            continue

        def adjusted(recipe: dict) -> tuple[float, str]:
            score = scores[recipe["id"]]
            if recipe.get("cuisine") in used_cuisines:
                score *= config.DIVERSITY_PENALTY
            # id is the tie-break so the same inputs always give the same plan
            return (-round(score, 6), recipe["id"])

        pick = min(candidates, key=adjusted)
        score = scores[pick["id"]]
        if pick.get("cuisine") in used_cuisines:
            score = round(score * config.DIVERSITY_PENALTY, 4)

        minutes = attended_minutes(pick)
        assignments.append({
            "recipe": pick,
            "slot": slot,
            "score": score,
            "planned_start_time": slot["slot_start"],
            "planned_end_time": (slot_start + timedelta(minutes=minutes)).isoformat(),
            "planned_date": slot_start.astimezone(config.TIMEZONE).date().isoformat(),
        })
        remaining.remove(pick)
        used_cuisines.add(pick.get("cuisine") or "")

    # A long braise may genuinely not fit any window this week. Report it
    # rather than dropping it silently — the user should know why it's missing.
    #
    # Only meaningful when windows exist: with no slots at all, nothing is
    # "too long", there's just no calendar data. Reporting every recipe as
    # oversized would be actively misleading.
    if not slots:
        return assignments, []

    longest = max(s["duration_minutes"] for s in slots)
    unschedulable = [
        r for r in remaining
        if attended_minutes(r) + config.SLOT_BUFFER_MINUTES > longest
    ]
    return assignments, unschedulable


# ---------------------------------------------------------------------------
# score_reason
# ---------------------------------------------------------------------------

def _template_reason(item: dict, pantry: dict[str, dict]) -> str:
    """Deterministic fallback. Never as good as the model, never fails."""
    recipe = item["recipe"]
    start = datetime.fromisoformat(item["planned_start_time"]).astimezone(config.TIMEZONE)
    expiring = expiring_ingredients(recipe, pantry)
    if expiring:
        name, days = expiring[0]
        when = "tomorrow" if days <= 1 else f"in {days} days"
        return f"Uses the {name} expiring {when}; fits the {start:%a} {start:%H:%M} window."
    return (f"Fits the {start:%a} {start:%H:%M} window "
            f"({attended_minutes(recipe)} min).")


def add_reasons(assignments: list[dict], pantry: dict[str, dict],
                use_llm: bool = True) -> None:
    """Attach score_reason to each assignment, in place.

    One batched call for the whole week: cheaper, faster, and a single eval_log
    row instead of five.
    """
    for item in assignments:
        item["score_reason"] = _template_reason(item, pantry)

    if not use_llm or not assignments:
        return

    lines = []
    for index, item in enumerate(assignments, start=1):
        recipe = item["recipe"]
        start = datetime.fromisoformat(
            item["planned_start_time"]).astimezone(config.TIMEZONE)
        expiring = expiring_ingredients(recipe, pantry)
        expiring_text = (", ".join(f"{n} expires in {d}d" for n, d in expiring[:3])
                         or "nothing expiring soon")
        lines.append(
            f"{index}. {recipe['title']} ({recipe.get('cuisine')}), "
            f"{attended_minutes(recipe)} min attended, scheduled "
            f"{start:%A %H:%M} in a {item['slot']['duration_minutes']}-minute "
            f"window. Pantry: {expiring_text}."
        )

    try:
        result = call_llm_structured(
            SCORE_REASON_PROMPT.format(meals="\n".join(lines)),
            MealReasons,
            stage=STAGE,
            input_ref="score_reason",
            validate=lambda r: (
                [] if len(r.reasons) == len(assignments)
                else [f"returned {len(r.reasons)} reasons for "
                      f"{len(assignments)} meals; return exactly one each"]
            ),
        )
    except LLMFailure:
        return          # templates are already in place

    for item, reason in zip(assignments, result.reasons):
        if reason and reason.strip():
            item["score_reason"] = reason.strip()


# ---------------------------------------------------------------------------
# the feedback edge: stage 5 -> stage 3
# ---------------------------------------------------------------------------

def delivery_conflict(week_start: date | None = None) -> dict | None:
    """Can the groceries actually arrive before the first meal?

    This is the one place the pipeline reconsiders its own output because the
    physical world said no. If the delivery window ends too late for the
    earliest cook slot, that slot is unusable and the meal has to move.

    Returns a description of the conflict, or None when the plan is feasible.
    Reads only database rows, so it never imports stage 5.
    """
    week_start = week_start or config.week_start()
    week = week_start.isoformat()

    orders = db.select("instacart_orders", "*", week_start_date=week)
    if not orders:
        return None                      # stage 5 hasn't run; nothing to check
    order = orders[0]
    if not order.get("delivery_window_end"):
        return None                      # advisory window only

    meals = db.select("meal_plan", "*", week_start_date=week)
    if not meals:
        return None

    earliest = min(meals, key=lambda m: m["planned_start_time"])
    starts_at = datetime.fromisoformat(earliest["planned_start_time"])
    delivery_end = datetime.fromisoformat(order["delivery_window_end"])
    deadline = starts_at - timedelta(hours=config.DELIVERY_BUFFER_HRS)

    if delivery_end <= deadline:
        return None

    recipe = db.get_recipe(earliest["recipe_id"]) or {}
    return {
        "slot_id": earliest["cook_slot_id"],
        "meal_id": earliest["id"],
        "title": recipe.get("title") or earliest["recipe_id"],
        "planned_start": starts_at,
        "delivery_end": delivery_end,
        "short_by_minutes": int((delivery_end - deadline).total_seconds() // 60),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(
    week_start: date | None = None,
    show_only: bool = False,
    use_llm: bool = True,
    exclude_slots: tuple[str, ...] = (),
) -> list[dict]:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()

    recipes = db.successful_recipes()
    slots = [s for s in db.select("cook_slots", "*", week_start_date=week)
             if s["id"] not in exclude_slots]
    pantry = usable_pantry()

    print(f"[3/6] plan          week of {week_start} | {len(recipes)} recipes, "
          f"{len(slots)} slots, {len(pantry)} usable pantry items")

    if not recipes:
        print("      no successful recipes - run extraction first")
        log_eval(STAGE, week, False, error_message="no recipes to plan")
        return []
    if not slots:
        print("      no cook_slots - run availability first")
        log_eval(STAGE, week, False, error_message="no cook_slots for the week")
        return []

    assignments, unschedulable = assign(recipes, slots, pantry)

    if not assignments:
        print("      nothing fits any window")
        log_eval(STAGE, week, False,
                 error_message="no recipe fits any available window")
        return []

    add_reasons(assignments, pantry, use_llm=use_llm)

    for item in assignments:
        start = datetime.fromisoformat(
            item["planned_start_time"]).astimezone(config.TIMEZONE)
        print(f"      {start:%a %d %H:%M}  {item['recipe']['title'][:30]:30s} "
              f"score {item['score']:.2f}  {item['score_reason']}")

    for recipe in unschedulable:
        print(f"      [skip] {recipe['title'][:30]:30s} needs "
              f"{attended_minutes(recipe) + config.SLOT_BUFFER_MINUTES} min - "
              f"longer than any window this week")

    if show_only:
        return assignments

    # Idempotent: drop the old plan and release the slots it held.
    db.delete_where("meal_plan", week_start_date=week)
    for slot in db.select("cook_slots", "id", week_start_date=week):
        db.update("cook_slots", slot["id"], {"assigned": False})

    rows = [{
        "recipe_id": item["recipe"]["id"],
        "cook_slot_id": item["slot"]["id"],
        "week_start_date": week,
        "planned_date": item["planned_date"],
        "planned_start_time": item["planned_start_time"],
        "planned_end_time": item["planned_end_time"],
        "score": item["score"],
        "score_reason": item["score_reason"],
        "status": "planned",
    } for item in assignments]

    written = db.insert("meal_plan", rows)
    for item in assignments:
        db.update("cook_slots", item["slot"]["id"], {"assigned": True})

    log_eval(STAGE, week, True)
    print(f"      wrote {len(written)} meal_plan rows")
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--show", action="store_true", help="don't write")
    parser.add_argument("--no-reasons", action="store_true",
                        help="skip the score_reason model call")
    parser.add_argument("--exclude-slot", action="append", default=[],
                        metavar="ID", help="slot id to leave unused (re-plan)")
    args = parser.parse_args()

    run(
        week_start=date.fromisoformat(args.week) if args.week else None,
        show_only=args.show,
        use_llm=not args.no_reasons,
        exclude_slots=tuple(args.exclude_slot),
    )
