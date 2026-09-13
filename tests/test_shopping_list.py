"""Stage 4 tests — consolidation, pantry subtraction, and unit handling.

Pure functions with fixture data: no network, no database. The unit rules are
the sharp edges here. Merging across a dimension would produce a silently wrong
cart, and refusing to merge within one produces a cart with the same
ingredient on it three times.

    python -m tests.test_shopping_list
"""

from __future__ import annotations

import unittest

from stages.shopping_list import required_totals, subtract_pantry

WEEK = "2026-09-14"


def meal(recipe_id: str) -> dict:
    return {"recipe_id": recipe_id, "week_start_date": WEEK}


def recipe(rid: str, *ingredients) -> dict:
    """recipe('r1', ('spinach', 400, 'g'), ('garlic', 2, 'unit'))"""
    return {
        "id": rid,
        "title": rid,
        "ingredients": [
            {"name": n, "quantity": q, "unit": u} for n, q, u in ingredients
        ],
    }


def index(*recipes) -> dict[str, dict]:
    return {r["id"]: r for r in recipes}


def pantry(*items) -> dict[str, dict]:
    """pantry(('spinach', 400, 'g'))"""
    return {
        n: {"ingredient_name": n, "quantity": q, "unit": u, "expiry_date": None}
        for n, q, u in items
    }


def buy(rows: list[dict]) -> dict[str, tuple[float, str]]:
    return {r["ingredient_name"]: (r["quantity_needed"], r["unit"]) for r in rows}


class Consolidation(unittest.TestCase):
    def test_same_ingredient_across_recipes_sums(self) -> None:
        totals = required_totals(
            [meal("r1"), meal("r2")],
            index(recipe("r1", ("garlic", 2, "unit")),
                  recipe("r2", ("garlic", 3, "unit"))),
        )
        self.assertEqual(totals[("garlic", "count")]["qty"], 5.0)

    def test_merges_within_a_dimension(self) -> None:
        """2 cup and 100 ml of milk is one purchase, not two cart lines."""
        totals = required_totals(
            [meal("r1"), meal("r2")],
            index(recipe("r1", ("milk", 2, "cup")),
                  recipe("r2", ("milk", 100, "ml"))),
        )
        self.assertEqual(len(totals), 1)
        self.assertAlmostEqual(totals[("milk", "volume")]["qty"], 573.176, places=2)

    def test_never_merges_across_dimensions(self) -> None:
        """Volume-to-mass depends on the ingredient, so butter stays separate."""
        totals = required_totals(
            [meal("r1"), meal("r2")],
            index(recipe("r1", ("butter", 3, "tbsp")),
                  recipe("r2", ("butter", 250, "g"))),
        )
        self.assertEqual(
            sorted(totals), [("butter", "mass"), ("butter", "volume")])

    def test_kg_and_g_merge(self) -> None:
        totals = required_totals(
            [meal("r1"), meal("r2")],
            index(recipe("r1", ("flour", 1, "kg")),
                  recipe("r2", ("flour", 500, "g"))),
        )
        self.assertEqual(totals[("flour", "mass")]["qty"], 1500.0)

    def test_non_food_excluded(self) -> None:
        totals = required_totals(
            [meal("r1")],
            index(recipe("r1", ("pasta water", 100, "ml"), ("spinach", 400, "g"))),
        )
        self.assertEqual(list(totals), [("spinach", "mass")])

    def test_null_and_zero_quantities_skipped(self) -> None:
        totals = required_totals(
            [meal("r1")],
            index(recipe("r1", ("salt", None, "tsp"), ("pepper", 0, "tsp"))),
        )
        self.assertEqual(totals, {})

    def test_missing_recipe_does_not_crash(self) -> None:
        """A meal referencing a recipe that isn't loaded must be skipped."""
        self.assertEqual(required_totals([meal("ghost")], index()), {})

    def test_no_meals_is_empty(self) -> None:
        self.assertEqual(required_totals([], index(recipe("r1"))), {})


class PantrySubtraction(unittest.TestCase):
    def test_fully_covered_is_not_bought(self) -> None:
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("spinach", 300, "g"))))
        rows, covered = subtract_pantry(totals, pantry(("spinach", 400, "g")))
        self.assertEqual(rows, [])
        self.assertEqual(covered, [("spinach", "mass")])

    def test_partial_stock_buys_only_the_shortfall(self) -> None:
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("spinach", 300, "g"))))
        rows, _ = subtract_pantry(totals, pantry(("spinach", 200, "g")))
        self.assertEqual(buy(rows), {"spinach": (100.0, "g")})

    def test_exactly_enough_is_covered(self) -> None:
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("spinach", 400, "g"))))
        rows, covered = subtract_pantry(totals, pantry(("spinach", 400, "g")))
        self.assertEqual(rows, [])
        self.assertEqual(len(covered), 1)

    def test_stock_in_a_different_dimension_does_not_count(self) -> None:
        """You have butter by weight, the recipe wants tablespoons. Buy it."""
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("butter", 3, "tbsp"))))
        rows, _ = subtract_pantry(totals, pantry(("butter", 250, "g")))
        self.assertEqual(buy(rows), {"butter": (3.0, "tbsp")})

    def test_stock_converts_within_a_dimension(self) -> None:
        """1 kg on hand against 1500 g needed leaves 500 g to buy."""
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("flour", 1500, "g"))))
        rows, _ = subtract_pantry(totals, pantry(("flour", 1, "kg")))
        self.assertEqual(buy(rows), {"flour": (500.0, "g")})

    def test_missing_from_pantry_buys_everything(self) -> None:
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("saffron", 1, "g"))))
        rows, covered = subtract_pantry(totals, pantry())
        self.assertEqual(buy(rows), {"saffron": (1.0, "g")})
        self.assertEqual(covered, [])

    def test_pantry_with_null_quantity_is_not_counted(self) -> None:
        totals = required_totals([meal("r1")],
                                 index(recipe("r1", ("spinach", 400, "g"))))
        store = pantry(("spinach", 400, "g"))
        store["spinach"]["quantity"] = None
        rows, _ = subtract_pantry(totals, store)
        self.assertEqual(buy(rows), {"spinach": (400.0, "g")})

    def test_never_both_bought_and_covered(self) -> None:
        """Carrot by weight in one recipe and by count in another.

        Listing it as bought AND covered reads as a contradiction on the cart.
        """
        totals = required_totals(
            [meal("r1"), meal("r2")],
            index(recipe("r1", ("carrot", 2, "unit")),
                  recipe("r2", ("carrot", 200, "g"))),
        )
        rows, covered = subtract_pantry(totals, pantry(("carrot", 4, "unit")))
        names_bought = {r["ingredient_name"] for r in rows}
        names_covered = {name for name, _ in covered}
        self.assertIn("carrot", names_bought)
        self.assertEqual(names_bought & names_covered, set())

    def test_every_row_is_ready_for_stage_5(self) -> None:
        totals = required_totals(
            [meal("r1")],
            index(recipe("r1", ("spinach", 400, "g"), ("garlic", 2, "unit"))))
        rows, _ = subtract_pantry(totals, pantry())
        for row in rows:
            self.assertEqual(row["resolution_status"], "pending")
            self.assertTrue(row["ingredient_name"])
            self.assertGreater(row["quantity_needed"], 0)
            self.assertTrue(row["unit"])


class UnitRendering(unittest.TestCase):
    """A cart reading '4.93 ml turmeric' is useless. Nobody buys spices by ml."""

    def render(self, *ingredients, stock=()):
        totals = required_totals([meal("r1")], index(recipe("r1", *ingredients)))
        rows, _ = subtract_pantry(totals, pantry(*stock))
        return buy(rows)

    def test_teaspoons_stay_teaspoons(self) -> None:
        self.assertEqual(self.render(("turmeric", 1, "tsp")),
                         {"turmeric": (1.0, "tsp")})

    def test_tablespoons_stay_tablespoons(self) -> None:
        self.assertEqual(self.render(("butter", 3, "tbsp")),
                         {"butter": (3.0, "tbsp")})

    def test_cups_stay_cups(self) -> None:
        self.assertEqual(self.render(("flour", 2, "cup")),
                         {"flour": (2.0, "cup")})

    def test_mixed_units_fall_back_to_the_base(self) -> None:
        """No single source unit to honour, so use something unambiguous."""
        result = self.render(("milk", 1, "cup"), ("milk", 50, "ml"))
        qty, unit = result["milk"]
        self.assertEqual(unit, "ml")
        self.assertAlmostEqual(qty, 286.59, places=1)

    def test_large_amounts_scale_up(self) -> None:
        self.assertEqual(self.render(("oil", 1530, "ml")), {"oil": (1.53, "l")})
        self.assertEqual(self.render(("flour", 2000, "g")), {"flour": (2.0, "kg")})

    def test_counts_render_bare(self) -> None:
        self.assertEqual(self.render(("egg", 2, "unit")), {"egg": (2.0, "unit")})

    def test_rows_are_sorted_by_name(self) -> None:
        totals = required_totals(
            [meal("r1")],
            index(recipe("r1", ("zucchini", 1, "unit"), ("apple", 1, "unit"))))
        rows, _ = subtract_pantry(totals, pantry())
        self.assertEqual([r["ingredient_name"] for r in rows],
                         ["apple", "zucchini"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
