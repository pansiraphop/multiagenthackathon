"""Integration test — stages 1 to 4 together, against live services.

Unlike the unit suites this costs real API calls and writes real rows, so it
is never picked up by `unittest discover`. Run it deliberately:

    python -m tests.test_pipeline            # run, then clean up
    python -m tests.test_pipeline --keep     # leave the rows for stage 3 work
    python -m tests.test_pipeline --offline  # skip Google, use fallback slots

What it actually proves is the handoffs. Each stage passing on its own doesn't
mean the next one can do anything: extraction has to produce recipes whose
ATTENDED time (total_time_minutes, not active) fits inside the windows
availability found, and the planner has to turn that into a schedule where
every block is the right length and no window is double-booked. Those joins
are the contract, and they're what this checks.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import config
from lib import db
from lib.evals import format_report
from lib.normalize import (
    is_non_food as _is_non_food,
    clean_source_text,
    dedupe_ingredients,
    is_non_food,
    render_amount,
    to_ingredient_row,
)
from lib.schemas import advance_prep, attended_minutes
from stages import availability, extract, plan, shopping_list
from seed import seed_pantry

INPUT_REF = "integration-test"

# Three cuisines, three durations. The short one must fit a narrow window and
# the long one must not — that's what makes the planner's job non-trivial.
CAPTIONS = [
    ("""10 MINUTE garlic butter noodles \U0001F35C save this!!
200g udon, 3 tbsp butter, 4 cloves garlic, 2 tbsp soy sauce,
1 tsp chilli flakes, handful of spring onion.
Boil noodles. Brown the butter with garlic. Toss with soy and chilli.
Serves 2. 10 mins flat!""", "short"),

    ("""Weeknight Palak Paneer \U0001F33F
400g spinach, 250g paneer, 2 onions, 3 cloves garlic, 1 inch ginger,
1 tsp garam masala, 1/2 tsp turmeric, a good glug of ghee, 100ml cream.
Blanch and blitz the spinach. Fry the paneer till golden. Bloom the spices,
add the puree, finish with cream. 35 minutes, serves 4.""", "medium"),

    ("""Korean fried chicken 🍗 marinate overnight, fry in 20 min
1 lb chicken wings, 3 tbsp gochujang, 2 tbsp soy sauce, 1 tbsp honey,
4 cloves garlic, 1 tbsp rice vinegar, 100g cornstarch, oil for frying.
Marinate the wings overnight in gochujang, soy, honey and garlic.
Next day dredge in cornstarch and fry 8 minutes a side. Serves 2.""", "marinade"),

    ("""SLOW BRAISED short rib ragu - worth every minute
1.5 lbs beef short rib, 2 carrots, 2 celery sticks, 1 onion,
400g tinned tomatoes, 250ml red wine, 2 bay leaves, olive oil.
Sear the beef. Soften the veg. Deglaze with wine, add tomatoes and bay.
Braise 2 hours. Shred and toss with pappardelle. Serves 4.""", "long"),
]


# Recorded from real extraction runs. Used by --fixtures so stages 2 and 3 stay
# testable when the model API is unavailable (expired key, no credits, outage).
# Ingredient names are deliberately the ones extraction actually produces, so
# the pantry-matching path is exercised exactly as it would be live.
FIXTURE_RECIPES = [
    dict(label="short", title="10 Minute Garlic Butter Noodles", cuisine="japanese",
         active=10, attended=10, lead=0, servings=2,
         steps=["Boil the noodles.", "Brown the butter with garlic.",
                "Toss with soy and chilli."],
         ingredients=[("udon", 200, "g"), ("butter", 3, "tbsp"),
                      ("garlic", 4, "unit"), ("soy sauce", 2, "tbsp"),
                      ("chilli flake", 1, "tsp"), ("spring onion", 1, "unit")]),

    dict(label="medium", title="Weeknight Palak Paneer", cuisine="indian",
         active=35, attended=35, lead=0, servings=4,
         steps=["Blanch and blitz the spinach.", "Fry the paneer until golden.",
                "Bloom the spices.", "Add the puree.", "Finish with cream."],
         ingredients=[("spinach", 400, "g"), ("paneer", 250, "g"),
                      ("onion", 2, "unit"), ("garlic", 3, "unit"),
                      ("ginger", 1, "unit"), ("garam masala", 1, "tsp"),
                      ("turmeric", 0.5, "tsp"), ("ghee", 2, "tbsp"),
                      ("cream", 100, "ml")]),

    dict(label="marinade", title="Korean Fried Chicken Wings", cuisine="korean",
         active=20, attended=30, lead=720, servings=2,
         steps=["Marinate the wings overnight.", "Dredge in cornstarch.",
                "Fry eight minutes a side."],
         ingredients=[("chicken wing", 453.59, "g"), ("gochujang", 3, "tbsp"),
                      ("soy sauce", 2, "tbsp"), ("honey", 1, "tbsp"),
                      ("garlic", 4, "unit"), ("rice vinegar", 1, "tbsp"),
                      ("cornstarch", 100, "g")]),

    dict(label="long", title="Slow Braised Short Rib Ragu", cuisine="italian",
         active=30, attended=150, lead=0, servings=4,
         steps=["Sear the beef.", "Soften the vegetables.", "Deglaze with wine.",
                "Braise for two hours.", "Shred and toss with pappardelle."],
         ingredients=[("beef short rib", 680, "g"), ("carrot", 2, "unit"),
                      ("celery", 2, "unit"), ("onion", 1, "unit"),
                      ("tinned tomato", 400, "g"), ("red wine", 250, "ml"),
                      ("bay leaf", 2, "unit"), ("olive oil", 2, "tbsp")]),
]


def insert_fixtures() -> list[dict]:
    """Write the recorded recipes straight to the database, no model call."""
    print("[1/6] extraction  (FIXTURES - no model call)")
    written = []
    for spec in FIXTURE_RECIPES:
        row = db.insert_recipe(
            raw_caption=f"fixture: {spec['title']}",
            source_url=f"https://instagram.com/reel/{INPUT_REF}-{spec['label']}",
            title=spec["title"], cuisine=spec["cuisine"],
            est_time_minutes=spec["active"],
            total_time_minutes=spec["attended"],
            advance_prep_minutes=spec["lead"],
            servings=spec["servings"], steps=spec["steps"],
            extraction_status="success", provenance="transcript",
            source_sufficiency="complete",
        )
        rows = dedupe_ingredients([
            to_ingredient_row(name, qty, unit)
            for name, qty, unit in spec["ingredients"]
        ])
        db.insert_ingredients(row["id"], rows)
        print(f"      {spec['title'][:32]:32s} active {spec['active']:>3} "
              f"attended {spec['attended']:>3}  {len(rows)} ingr")
        written.append(row)
    return written


def fail(message: str) -> None:
    print(f"  FAIL: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------

def run_stage_1() -> list[dict]:
    """Insert pending rows the way ingestion does, then run the REAL extractor.

    Deliberately calls stages.extract.extract_recipe rather than reimplementing
    extraction here — otherwise this tests a copy of stage 1, not stage 1.
    """
    print("[1/6] extraction")
    written: list[dict] = []

    for caption, label in CAPTIONS:
        pending = db.insert_recipe(
            raw_caption=clean_source_text(caption),
            source_url=f"https://instagram.com/reel/{INPUT_REF}-{label}",
            extraction_status="pending",
        )
        result = extract.extract_recipe(pending)
        if result is None:
            fail(f"{label}: extractor failed and marked the row failed")
        written.append(result)

    if len(written) != len(CAPTIONS):
        fail(f"expected {len(CAPTIONS)} recipes, wrote {len(written)}")
    return written


def check_stage_1_invariants() -> list[dict]:
    """Whatever extraction wrote has to be safe for the planner to consume."""
    recipes = [r for r in db.successful_recipes()
               if (r.get("source_url") or "").find(INPUT_REF) >= 0]
    if not recipes:
        fail("successful_recipes() returned none of the test recipes")

    for recipe in recipes:
        if not recipe["est_time_minutes"]:
            fail(f"{recipe['title']}: est_time_minutes is null - the planner's "
                 f"slot-fitting comparison would raise")
        if not recipe.get("total_time_minutes"):
            fail(f"{recipe['title']}: total_time_minutes is null - the planner "
                 f"fits on attended time, so this must be set")
        if recipe["total_time_minutes"] < recipe["est_time_minutes"]:
            fail(f"{recipe['title']}: attended span is shorter than the active "
                 f"work inside it")
        if not recipe["ingredients"]:
            fail(f"{recipe['title']}: no ingredient rows")
        if not recipe["steps"]:
            fail(f"{recipe['title']}: no steps, so the calendar event is empty")

        for ing in recipe["ingredients"]:
            if ing["name"] != ing["name"].lower():
                fail(f"{recipe['title']}: '{ing['name']}' was not normalized")
            if ing["unit"] not in config.UNITS:
                fail(f"{recipe['title']}: unit '{ing['unit']}' is outside the enum")
            if ing["quantity"] is None and not ing["qualitative_note"]:
                fail(f"{recipe['title']}: '{ing['name']}' has no usable amount")
            if ing["is_approximate"] and not ing["qualitative_note"]:
                fail(f"{recipe['title']}: '{ing['name']}' is approximate but "
                     f"unlabelled - a cook can't tell estimate from measurement")

    print(f"      invariants OK across {len(recipes)} recipes")
    return recipes


def run_stage_2(offline: bool) -> list[dict]:
    slots = availability.run(offline=offline)
    if not slots:
        fail("availability wrote no cook_slots")
    return slots


def run_stage_3(use_llm: bool = True) -> list[dict]:
    rows = plan.run(use_llm=use_llm)
    if not rows:
        fail("planner produced no meal_plan rows")
    return rows


def check_plan_invariants(meals: list[dict], slots: list[dict],
                          recipes: list[dict]) -> None:
    """Whatever the planner wrote has to be safe for calendar_sync to consume."""
    print("[2+3] plan invariants")

    by_slot = {s["id"]: s for s in slots}
    # The planner works across EVERY successful recipe, not just the ones this
    # test inserted — real ingested reels are in the pool too. Load them all,
    # or the invariant check trips over a meal it doesn't recognise.
    all_recipes = db.successful_recipes()
    by_recipe = {r["id"]: r for r in all_recipes}
    pantry = plan.usable_pantry()

    for row in meals:
        if row["recipe_id"] not in by_recipe:
            fail(f"meal references recipe {row['recipe_id']} which is not in "
                 f"successful_recipes()")

    seen_slots, seen_recipes = set(), set()
    for row in meals:
        title = (by_recipe.get(row["recipe_id"]) or {}).get("title", row["recipe_id"])

        if row["cook_slot_id"] not in by_slot:
            fail(f"{title}: cook_slot_id points at no slot from this week")
        if row["cook_slot_id"] in seen_slots:
            fail(f"{title}: window double-booked")
        if row["recipe_id"] in seen_recipes:
            fail(f"{title}: planned twice in one week")
        seen_slots.add(row["cook_slot_id"])
        seen_recipes.add(row["recipe_id"])

        start = datetime.fromisoformat(row["planned_start_time"])
        end = datetime.fromisoformat(row["planned_end_time"])
        block = (end - start).total_seconds() / 60
        expected = attended_minutes(by_recipe[row["recipe_id"]])
        if block != expected:
            fail(f"{title}: calendar block is {block:.0f} min but the recipe needs "
                 f"{expected} attended - the event would end mid-cook")

        window = by_slot[row["cook_slot_id"]]
        if start.isoformat() != window["slot_start"]:
            fail(f"{title}: does not start when its window does")
        if expected + config.SLOT_BUFFER_MINUTES > window["duration_minutes"]:
            fail(f"{title}: needs {expected} min in a "
                 f"{window['duration_minutes']} min window")

        if not (row.get("score_reason") or "").strip():
            fail(f"{title}: empty score_reason - the calendar invite has no 'why'")
        if row["status"] != "planned":
            fail(f"{title}: status is {row['status']}, expected 'planned'")

    # The flags calendar_sync and the re-plan loop both rely on.
    assigned = {s["id"] for s in db.select("cook_slots", "*",
                                           week_start_date=config.week_start().isoformat())
                if s["assigned"]}
    if assigned != seen_slots:
        fail(f"cook_slots.assigned is out of step with meal_plan "
             f"({len(assigned)} flagged, {len(seen_slots)} planned)")

    print(f"      {len(meals)} meals, no double-booking, every block the right length")

    # The demo claim: urgent food gets cooked first.
    ordered = sorted(meals, key=lambda r: r["planned_start_time"])
    first = by_recipe[ordered[0]["recipe_id"]]
    urgent_anywhere = [r for r in recipes if plan.expiring_ingredients(r, pantry)]
    if urgent_anywhere:
        soon = plan.expiring_ingredients(first, pantry)
        if soon:
            name, days = soon[0]
            print(f"      earliest meal rescues '{name}' ({days}d left) - "
                  f"expiry urgency is driving the order")
        else:
            print(f"      [warn] the earliest meal uses nothing expiring, but "
                  f"{len(urgent_anywhere)} recipe(s) do - check the weights")

    stages_logged = {row["stage"] for row in db.select("eval_log")}
    if "planning" not in stages_logged:
        fail("eval_log has no 'planning' rows")


def run_stage_4() -> list[dict]:
    rows = shopping_list.run()
    if not rows:
        fail("shopping list is empty - every planned ingredient can't already "
             "be in the pantry")
    return rows


def check_shopping_invariants(items: list[dict], meals: list[dict],
                              recipes: list[dict]) -> None:
    """What stage 5 will pick up has to be buyable and unambiguous."""
    print("[3+4] shopping invariants")

    by_recipe = {r["id"]: r for r in db.successful_recipes()}
    planned = {m["recipe_id"] for m in meals}

    seen: set[tuple[str, str]] = set()
    for row in items:
        name = row["ingredient_name"]

        if row["resolution_status"] != "pending":
            fail(f"{name}: status is {row['resolution_status']}, stage 5 only "
                 f"picks up 'pending'")
        if row["quantity_needed"] is None or float(row["quantity_needed"]) <= 0:
            fail(f"{name}: quantity_needed is {row['quantity_needed']}")
        if not row["unit"]:
            fail(f"{name}: no unit")
        if _is_non_food(name):
            fail(f"{name}: non-food reached the cart")

        key = (name, row["unit"])
        if key in seen:
            fail(f"{name} ({row['unit']}): duplicated - violates the "
                 f"(week, name, unit) unique constraint")
        seen.add(key)

    # Everything on the list must come from a recipe that's actually planned.
    wanted = {
        ing["name"]
        for rid in planned
        for ing in by_recipe.get(rid, {}).get("ingredients", [])
    }
    for row in items:
        if row["ingredient_name"] not in wanted:
            fail(f"{row['ingredient_name']}: on the cart but in no planned recipe")

    # The whole point of the stage: the cart is the gap, not the recipe.
    distinct_wanted = len({n for n in wanted if not _is_non_food(n)})
    if len(items) >= distinct_wanted:
        fail(f"{len(items)} items for {distinct_wanted} ingredients - the pantry "
             f"subtracted nothing, so the demo has no story")
    print(f"      {len(items)} to buy out of {distinct_wanted} ingredients "
          f"across {len(meals)} meals - pantry covered the rest")

    stages_logged = {row["stage"] for row in db.select("eval_log")}
    if "shopping_list" not in stages_logged:
        fail("eval_log has no 'shopping_list' rows")


def check_handoff(recipes: list[dict], slots: list[dict],
                  expect_extraction: bool = True) -> None:
    """The actual integration claim: the planner has something to work with."""
    print("[1+2] handoff")

    buffer = config.SLOT_BUFFER_MINUTES
    durations = sorted(s["duration_minutes"] for s in slots)
    longest = durations[-1]

    fits_any = 0
    unschedulable: list[str] = []
    for recipe in sorted(recipes, key=lambda r: attended_minutes(r)):
        need = attended_minutes(recipe) + buffer
        usable = [d for d in durations if need <= d]
        verdict = (f"fits {len(usable)}/{len(durations)} windows" if usable
                   else "fits NO window")
        lead = advance_prep(recipe)
        note = f"  (start {lead // 60}h ahead)" if lead else ""
        print(f"      {recipe['title'][:32]:32s} needs {need:>3} min  {verdict}{note}")
        if usable:
            fits_any += 1
        else:
            unschedulable.append(recipe["title"])

    # Not a failure - a long braise genuinely may not fit this week. But the
    # planner has to report it rather than silently dropping the recipe.
    if unschedulable:
        print(f"      [note] {len(unschedulable)} recipe(s) exceed every window; "
              f"the planner must surface these, not drop them silently")

    if fits_any == 0:
        fail(f"no recipe fits any window (longest is {longest} min) - the planner "
             f"would produce an empty plan")

    # The narrow windows are the point of the whole design. If every recipe fits
    # everywhere, the constraint isn't doing any work and the demo has no story.
    narrow = [d for d in durations if d < 45]
    if narrow:
        excluded = [r["title"] for r in recipes
                    if attended_minutes(r) + buffer > narrow[-1]]
        if not excluded:
            print(f"      [warn] every recipe fits the {narrow[-1]}-min window; "
                  f"the fit constraint isn't being exercised")
        else:
            print(f"      {len(narrow)} narrow window(s) exclude "
                  f"{len(excluded)} recipe(s) - fit constraint is live")

    # Both stages must have left an audit trail; it's a graded deliverable.
    stages_logged = {row["stage"] for row in db.select("eval_log")}
    required = ["availability"] + (["extraction"] if expect_extraction else [])
    for stage in required:
        if stage not in stages_logged:
            fail(f"eval_log has no '{stage}' rows")
    note = "" if expect_extraction else "  (fixtures: no extraction rows expected)"
    print(f"      eval_log covers: {', '.join(sorted(stages_logged))}{note}")


def show_sample(recipes: list[dict]) -> None:
    """What a calendar description will actually look like."""
    recipe = max(recipes, key=lambda r: len(r["ingredients"]))
    print()
    print(f"  sample calendar body - {recipe['title']}")
    for ing in recipe["ingredients"]:
        if is_non_food(ing["name"]):
            continue
        row = {
            "quantity": float(ing["quantity"]) if ing["quantity"] is not None else None,
            "unit": ing["unit"],
            "qualitative_note": ing["qualitative_note"],
            "is_approximate": ing["is_approximate"],
        }
        print(f"      {ing['name']:22s} {render_amount(row)}")


def cleanup() -> None:
    db.delete_where("meal_plan", week_start_date=config.week_start().isoformat())
    for recipe in db.select("recipes"):
        if INPUT_REF in (recipe.get("source_url") or ""):
            db.delete_where("recipes", id=recipe["id"])   # cascades ingredients
    db.delete_where("cook_slots", week_start_date=config.week_start().isoformat())
    db.delete_where("eval_log", input_ref=INPUT_REF)
    db.delete_where("eval_log", stage="availability")
    db.delete_where("eval_log", stage="planning")
    db.delete_where("eval_log", stage="shopping_list")
    db.delete_where("shopping_list", week_start_date=config.week_start().isoformat())
    print("  cleaned up test rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the rows in place for stage 3 work")
    parser.add_argument("--offline", action="store_true",
                        help="skip Google and use fallback slots")
    parser.add_argument("--fixtures", action="store_true",
                        help="use recorded recipes instead of calling the model; "
                             "keeps stages 2 and 3 testable with no API access")
    args = parser.parse_args()

    started = datetime.now()
    mode = " (fixtures)" if args.fixtures else ""
    print(f"integration: stages 1-4{mode}, week of {config.week_start()}")
    print()

    cleanup()   # start from a known state, not yesterday's leftovers
    seed_pantry.seed()   # the planner needs expiry data to score on
    print()

    if args.fixtures:
        insert_fixtures()
    else:
        run_stage_1()
    recipes = check_stage_1_invariants()
    print()
    slots = run_stage_2(args.offline)
    print()
    check_handoff(recipes, slots, expect_extraction=not args.fixtures)
    print()
    meals = run_stage_3(use_llm=not args.fixtures)
    print()
    check_plan_invariants(meals, slots, recipes)
    print()
    items = run_stage_4()
    print()
    check_shopping_invariants(items, meals, recipes)
    show_sample(recipes)

    print()
    print(format_report())
    print()

    if args.keep:
        print(f"  kept {len(recipes)} recipes, {len(slots)} cook_slots, "
              f"{len(meals)} meals, {len(items)} shopping items")
    else:
        cleanup()

    print(f"\nPASS in {(datetime.now() - started).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
