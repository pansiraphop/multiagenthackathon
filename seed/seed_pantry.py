"""Seed the pantry with a realistic fridge.

This is the demo's "why did it pick these meals" evidence. The planner weights
expiry urgency highest, so without varied expiry dates there is nothing to
show: every recipe scores the same and the plan looks arbitrary.

Designed so the story lands:
  · chicken thigh expires tomorrow, spinach in two days -> those get scheduled
    first, in the earliest windows
  · mid-range produce gives the middle of the week something to work with
  · staples with no expiry keep the shopping list short, so the cart shows only
    what's genuinely missing
  · one already-expired item, to prove expired stock is ignored rather than
    treated as urgent

Names go through normalize_name() on the way in. If the pantry says "Tomatoes"
and a recipe says "tomato", nothing matches and the whole thing quietly fails.

    python -m seed.seed_pantry            # replace the pantry
    python -m seed.seed_pantry --list     # show what's there
    python -m seed.seed_pantry --clear    # empty it
"""

from __future__ import annotations

import argparse
from datetime import timedelta

import config
from lib import db
from lib.normalize import normalize_ingredient

# (name, quantity, unit, days until expiry or None for a staple)
PANTRY = [
    # --- about to go off: this is what drives the plan -------------------
    ("chicken thighs", 600, "g", 1),
    ("spinach", 400, "g", 2),
    ("coriander", 1, "unit", 2),
    ("spring onions", 1, "unit", 3),

    # --- this week ------------------------------------------------------
    ("paneer", 250, "g", 4),
    ("double cream", 200, "ml", 5),
    ("celery", 1, "unit", 8),
    ("carrots", 4, "unit", 10),

    # --- comfortable ----------------------------------------------------
    ("ginger", 100, "g", 14),
    ("butter", 250, "g", 20),
    ("onions", 5, "unit", 21),
    ("garlic", 12, "unit", 30),
    ("udon noodles", 400, "g", 60),

    # --- already gone: must be ignored, not treated as urgent -----------
    ("greek yoghurt", 200, "g", -3),

    # --- staples, no expiry ---------------------------------------------
    ("olive oil", 500, "ml", None),
    ("soy sauce", 250, "ml", None),
    ("rice vinegar", 250, "ml", None),
    ("honey", 340, "g", None),
    ("gochujang", 200, "g", None),
    ("cornstarch", 400, "g", None),
    ("plain flour", 1, "kg", None),
    ("rice", 2, "kg", None),
    ("sugar", 500, "g", None),
    ("salt", 500, "g", None),
    ("black pepper", 50, "g", None),
    ("garam masala", 50, "g", None),
    ("turmeric", 50, "g", None),
    ("chilli flakes", 50, "g", None),
    ("bay leaves", 10, "unit", None),
]


def rows() -> list[dict]:
    today = config.now_local().date()
    built = []
    for raw_name, qty, unit, days in PANTRY:
        name, quantity, clean_unit = normalize_ingredient(raw_name, qty, unit)
        built.append({
            "ingredient_name": name,
            "quantity": quantity,
            "unit": clean_unit,
            "expiry_date": (today + timedelta(days=days)).isoformat()
                           if days is not None else None,
        })
    return built


def seed() -> list[dict]:
    built = rows()

    names = [r["ingredient_name"] for r in built]
    if len(names) != len(set(names)):
        dupes = {n for n in names if names.count(n) > 1}
        raise SystemExit(f"duplicate pantry names after normalization: {dupes}")

    # Replace wholesale: re-running must not double the fridge.
    db.delete_all("pantry")
    written = db.insert("pantry", built)

    today = config.now_local().date()
    urgent = [r for r in written
              if r["expiry_date"] and r["expiry_date"] <= (today + timedelta(days=3)).isoformat()]
    expired = [r for r in written
               if r["expiry_date"] and r["expiry_date"] < today.isoformat()]
    staples = [r for r in written if not r["expiry_date"]]

    print(f"seeded {len(written)} pantry items")
    print(f"  {len(urgent)} expiring within 3 days, {len(staples)} staples, "
          f"{len(expired)} already expired (must be ignored by the planner)")
    for row in sorted(urgent, key=lambda r: r["expiry_date"]):
        print(f"    {row['ingredient_name']:18s} expires {row['expiry_date']}")
    return written


def show() -> None:
    today = config.now_local().date().isoformat()
    for row in sorted(db.select("pantry"),
                      key=lambda r: (r["expiry_date"] or "9999-99-99")):
        expiry = row["expiry_date"] or "staple"
        flag = "  EXPIRED" if row["expiry_date"] and row["expiry_date"] < today else ""
        qty = f"{float(row['quantity']):g}" if row["quantity"] is not None else "?"
        print(f"  {row['ingredient_name']:18s} {qty:>6s} {row['unit']:5s} {expiry}{flag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true", help="empty the pantry")
    parser.add_argument("--list", action="store_true", help="show current contents")
    args = parser.parse_args()

    if args.clear:
        db.delete_all("pantry")
        print("pantry cleared")
    elif args.list:
        show()
    else:
        seed()
