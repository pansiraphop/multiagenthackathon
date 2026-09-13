"""Integration test — stages 1 and 2 together, against live services.

Unlike the unit suites this costs real API calls and writes real rows, so it
is never picked up by `unittest discover`. Run it deliberately:

    python -m tests.test_pipeline            # run, then clean up
    python -m tests.test_pipeline --keep     # leave the rows for stage 3 work
    python -m tests.test_pipeline --offline  # skip Google, use fallback slots

What it actually proves is the handoff. Each stage passing on its own doesn't
mean the planner can do anything: extraction has to produce recipes whose
est_time_minutes fit inside the windows availability found. That join is the
contract, and it's what this checks.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import config
from lib import db
from lib.evals import format_report
from lib.llm import call_llm_structured
from lib.normalize import (
    clean_source_text,
    dedupe_ingredients,
    is_non_food,
    render_amount,
    to_ingredient_row,
)
from lib.prompts import EXTRACTION_PROMPT, EXTRACTION_SYSTEM
from lib.schemas import ExtractedRecipe, needs_reconstruction, validate_recipe
from stages import availability

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

    ("""SLOW BRAISED short rib ragu - worth every minute
1.5 lbs beef short rib, 2 carrots, 2 celery sticks, 1 onion,
400g tinned tomatoes, 250ml red wine, 2 bay leaves, olive oil.
Sear the beef. Soften the veg. Deglaze with wine, add tomatoes and bay.
Braise 2 hours. Shred and toss with pappardelle. Serves 4.""", "long"),
]


def fail(message: str) -> None:
    print(f"  FAIL: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------

def run_stage_1() -> list[dict]:
    """Extract each caption and write recipes + recipe_ingredients."""
    print("[1/6] extraction")
    written: list[dict] = []

    for caption, label in CAPTIONS:
        source = clean_source_text(caption)
        parsed = call_llm_structured(
            EXTRACTION_PROMPT.format(source_text=source),
            ExtractedRecipe,
            stage="extraction",
            input_ref=INPUT_REF,
            validate=validate_recipe,
            system=EXTRACTION_SYSTEM,
        )

        recipe = db.insert_recipe(
            raw_caption=source,
            source_url=f"https://instagram.com/reel/{INPUT_REF}-{label}",
            title=parsed.title,
            cuisine=parsed.cuisine,
            est_time_minutes=parsed.est_time_minutes,
            servings=parsed.servings,
            steps=parsed.steps,
            extraction_status="success",
            provenance="reconstructed" if needs_reconstruction(parsed) else "transcript",
            source_sufficiency=parsed.source_sufficiency,
        )

        rows = dedupe_ingredients([
            to_ingredient_row(i.name, i.quantity, i.unit,
                              i.qualitative_note, i.is_approximate)
            for i in parsed.ingredients
        ])
        db.insert_ingredients(recipe["id"], rows)

        approx = sum(1 for r in rows if r["is_approximate"])
        print(f"      {parsed.title[:34]:34s} {parsed.est_time_minutes:>3} min  "
              f"{len(rows)} ingredients ({approx} approximated)")
        written.append(recipe)

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


def check_handoff(recipes: list[dict], slots: list[dict]) -> None:
    """The actual integration claim: the planner has something to work with."""
    print("[1+2] handoff")

    buffer = config.SLOT_BUFFER_MINUTES
    durations = sorted(s["duration_minutes"] for s in slots)
    longest = durations[-1]

    fits_any = 0
    for recipe in sorted(recipes, key=lambda r: r["est_time_minutes"]):
        need = recipe["est_time_minutes"] + buffer
        usable = [d for d in durations if need <= d]
        verdict = f"fits {len(usable)}/{len(durations)} windows" if usable else "fits NO window"
        print(f"      {recipe['title'][:34]:34s} needs {need:>3} min  {verdict}")
        if usable:
            fits_any += 1

    if fits_any == 0:
        fail(f"no recipe fits any window (longest is {longest} min) - the planner "
             f"would produce an empty plan")

    # The narrow windows are the point of the whole design. If every recipe fits
    # everywhere, the constraint isn't doing any work and the demo has no story.
    narrow = [d for d in durations if d < 45]
    if narrow:
        excluded = [r["title"] for r in recipes
                    if r["est_time_minutes"] + buffer > narrow[-1]]
        if not excluded:
            print(f"      [warn] every recipe fits the {narrow[-1]}-min window; "
                  f"the fit constraint isn't being exercised")
        else:
            print(f"      {len(narrow)} narrow window(s) exclude "
                  f"{len(excluded)} recipe(s) - fit constraint is live")

    # Both stages must have left an audit trail; it's a graded deliverable.
    stages_logged = {row["stage"] for row in db.select("eval_log")}
    for required in ("extraction", "availability"):
        if required not in stages_logged:
            fail(f"eval_log has no '{required}' rows")
    print(f"      eval_log covers: {', '.join(sorted(stages_logged))}")


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
    for recipe in db.select("recipes"):
        if INPUT_REF in (recipe.get("source_url") or ""):
            db.delete_where("recipes", id=recipe["id"])   # cascades ingredients
    db.delete_where("cook_slots", week_start_date=config.week_start().isoformat())
    db.delete_where("eval_log", input_ref=INPUT_REF)
    db.delete_where("eval_log", stage="availability")
    print("  cleaned up test rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="leave the rows in place for stage 3 work")
    parser.add_argument("--offline", action="store_true",
                        help="skip Google and use fallback slots")
    args = parser.parse_args()

    started = datetime.now()
    print(f"integration: stages 1 + 2, week of {config.week_start()}")
    print()

    cleanup()   # start from a known state, not yesterday's leftovers
    print()

    recipes = run_stage_1()
    recipes = check_stage_1_invariants()
    print()
    slots = run_stage_2(args.offline)
    print()
    check_handoff(recipes, slots)
    show_sample(recipes)

    print()
    print(format_report())
    print()

    if args.keep:
        print(f"  kept {len(recipes)} recipes and {len(slots)} cook_slots")
    else:
        cleanup()

    print(f"\nPASS in {(datetime.now() - started).total_seconds():.1f}s")


if __name__ == "__main__":
    main()
