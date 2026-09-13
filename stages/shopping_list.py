"""Stage 4 — what you actually need to buy this week.

Sums every planned recipe's ingredients, subtracts what's already in the
fridge, and writes what's left. This is the stage that makes the demo land:
the cart shows only the gap, not the whole recipe.

    python -m stages.shopping_list
    python -m stages.shopping_list --show      # print, don't write
    python -m stages.shopping_list --verbose   # show what the pantry covered

Reads meal_plan + recipe_ingredients + pantry. Writes shopping_list with
resolution_status='pending', which is what stage 5 picks up.
Idempotent: clears the week's list first.
"""

from __future__ import annotations

import argparse
from datetime import date

import config
from lib import db
from lib.evals import log_eval
from lib.normalize import (
    base_to_unit,
    dimension_of,
    from_base_unit,
    is_non_food,
    normalize_unit,
    to_base_unit,
)
from stages.plan import usable_pantry

STAGE = "shopping_list"


def required_totals(meals: list[dict], recipes: dict[str, dict]
                    ) -> dict[tuple[str, str], dict]:
    """Sum the week's requirements, keyed by (name, dimension).

    Keying on dimension rather than the literal unit is what lets "2 cup" and
    "100 ml" of the same thing merge into one purchase. Mass and volume never
    merge, because that conversion depends on the ingredient.
    """
    totals: dict[tuple[str, str], dict] = {}
    for meal in meals:
        recipe = recipes.get(meal["recipe_id"])
        if not recipe:
            continue
        for ing in recipe.get("ingredients", []):
            name = ing["name"]
            if not name or is_non_food(name):
                continue                      # water and ice are not groceries
            qty, _base = to_base_unit(
                float(ing["quantity"]) if ing["quantity"] is not None else None,
                ing["unit"],
            )
            if qty is None or qty <= 0:
                continue
            key = (name, dimension_of(ing["unit"]))
            entry = totals.setdefault(key, {"qty": 0.0, "units": set()})
            entry["qty"] = round(entry["qty"] + qty, 4)
            # Remember how the recipes actually expressed it, so the cart can
            # say "1 tsp turmeric" instead of "4.93 ml".
            entry["units"].add(normalize_unit(ing["unit"]))
    return totals


def subtract_pantry(
    totals: dict[tuple[str, str], dict],
    pantry: dict[str, dict],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return (rows to buy, items the pantry fully covered).

    Pantry stock only counts when it's the same dimension. A recipe wanting
    cups of something you have by weight is a genuine unknown, so we buy the
    full amount and say so in the brief — a stated limitation beats a silently
    wrong converter.
    """
    to_buy: list[dict] = []
    covered: list[tuple[str, str]] = []

    for (name, dim), entry in sorted(totals.items()):
        needed, source_units = entry["qty"], entry["units"]

        have = 0.0
        stock = pantry.get(name)
        if stock and stock.get("quantity") is not None:
            if dimension_of(stock.get("unit")) == dim:
                have, _ = to_base_unit(float(stock["quantity"]), stock["unit"])
                have = have or 0.0

        short = round(needed - have, 4)
        if short <= 0:
            covered.append((name, dim))
            continue

        # Render in the recipes' own unit when they all agreed; otherwise fall
        # back to the readable base (scaling g->kg, ml->l for large amounts).
        base = {"mass": "g", "volume": "ml", "count": "unit"}[dim]
        if len(source_units) == 1:
            qty, unit = base_to_unit(short, next(iter(source_units)))
        else:
            qty, unit = from_base_unit(short, base)
        # Large amounts read better scaled up, whichever branch produced them.
        if unit in ("g", "ml") and qty is not None and qty >= 1000:
            qty, unit = from_base_unit(short, base)

        to_buy.append({
            "ingredient_name": name,
            "quantity_needed": qty,
            "unit": unit,
            "resolution_status": "pending",
        })

    # An ingredient listed as BOTH bought and covered reads as a contradiction.
    # It happens legitimately when one recipe wants it by weight and another by
    # count, but the cart should just show the shortfall.
    buying = {row["ingredient_name"] for row in to_buy}
    covered = [(name, dim) for name, dim in covered if name not in buying]

    return to_buy, covered


def build(week_start: date) -> tuple[list[dict], list[tuple[str, str]], int]:
    week = week_start.isoformat()
    meals = db.select("meal_plan", "*", week_start_date=week)
    if not meals:
        return [], [], 0

    recipes = {r["id"]: r for r in db.successful_recipes()}
    pantry = usable_pantry()

    totals = required_totals(meals, recipes)
    to_buy, covered = subtract_pantry(totals, pantry)
    return to_buy, covered, len(totals)


def run(week_start: date | None = None, show_only: bool = False,
        verbose: bool = False) -> list[dict]:
    week_start = week_start or config.week_start()
    week = week_start.isoformat()

    to_buy, covered, distinct = build(week_start)

    if not distinct:
        print("[4/6] shopping      no meal_plan for this week - run the planner first")
        log_eval(STAGE, week, False, error_message="no meal_plan rows")
        return []

    print(f"[4/6] shopping      {distinct} ingredients across the week -> "
          f"{len(to_buy)} to buy ({len(covered)} already in the pantry)")

    for row in to_buy:
        qty = f"{row['quantity_needed']:g}"
        amount = qty if row["unit"] == "unit" else f"{qty} {row['unit']}"
        print(f"      {row['ingredient_name']:24s} {amount}")

    if verbose and covered:
        print("      --- covered by the pantry ---")
        for name, _dim in covered:
            print(f"      {name}")

    if show_only:
        return to_buy

    db.delete_where("shopping_list", week_start_date=week)
    rows = [dict(r, week_start_date=week) for r in to_buy]
    written = db.insert("shopping_list", rows) if rows else []

    log_eval(STAGE, week, True)
    print(f"      wrote {len(written)} shopping_list rows")
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--show", action="store_true", help="don't write")
    parser.add_argument("--verbose", action="store_true",
                        help="also list what the pantry covered")
    args = parser.parse_args()

    run(
        week_start=date.fromisoformat(args.week) if args.week else None,
        show_only=args.show,
        verbose=args.verbose,
    )
