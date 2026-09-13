"""Stage 1 tests — the recipe contract, with the timing rules front and centre.

No network, no database. The timing cases are the important ones: a recipe's
attended span is what books the cook's evening, so a braise recorded as active
time gets scheduled into a gap it cannot possibly fit.

    python -m tests.test_extraction
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from lib.normalize import (
    dedupe_ingredients,
    is_non_food,
    normalize_measure,
    parse_quantity,
    render_amount,
    to_ingredient_row,
)
from stages.extract import _extract_from_source
from lib.schemas import (
    DishIdentification,
    ExtractedRecipe,
    Ingredient,
    advance_prep,
    attended_minutes,
    needs_reconstruction,
    validate_recipe,
)


def recipe(**overrides) -> ExtractedRecipe:
    """A valid baseline recipe. Override one field per test."""
    base = dict(
        title="Weeknight Palak Paneer",
        cuisine="indian",
        est_time_minutes=35,
        total_time_minutes=35,
        advance_prep_minutes=0,
        servings=4,
        steps=["Blanch the spinach.", "Fry the paneer.", "Simmer together."],
        ingredients=[
            Ingredient(name="spinach", quantity=400, unit="g"),
            Ingredient(name="paneer", quantity=250, unit="g"),
            Ingredient(name="ghee", quantity=2, unit="tbsp",
                       is_approximate=True, qualitative_note="a good glug"),
        ],
        source_sufficiency="complete",
    )
    base.update(overrides)
    return ExtractedRecipe(**base)


class BaselineIsValid(unittest.TestCase):
    def test_no_problems(self) -> None:
        self.assertEqual(validate_recipe(recipe()), [])

    def test_no_reconstruction_needed(self) -> None:
        self.assertFalse(needs_reconstruction(recipe()))


class AttendedTime(unittest.TestCase):
    """total_time_minutes is what the planner books. These guard that."""

    def test_quick_dish_active_equals_attended(self) -> None:
        r = recipe(est_time_minutes=10, total_time_minutes=10)
        self.assertEqual(validate_recipe(r), [])
        self.assertEqual(attended_minutes(r), 10)

    def test_braise_keeps_the_long_span(self) -> None:
        """Sear 30 min then braise 2 h: the cook is home for 150 minutes."""
        r = recipe(est_time_minutes=30, total_time_minutes=150)
        self.assertEqual(validate_recipe(r), [])
        self.assertEqual(attended_minutes(r), 150)

    def test_attended_cannot_be_shorter_than_active(self) -> None:
        problems = validate_recipe(recipe(est_time_minutes=40, total_time_minutes=20))
        self.assertTrue(any("less than" in p for p in problems))

    def test_attended_upper_bound(self) -> None:
        problems = validate_recipe(
            recipe(total_time_minutes=config.TOTAL_TIME_MAX + 1))
        self.assertTrue(any("total_time_minutes" in p for p in problems))

    def test_long_braise_is_allowed_where_active_time_would_not_be(self) -> None:
        """300 min attended is fine; 300 min of knife work is not."""
        self.assertEqual(validate_recipe(recipe(total_time_minutes=300)), [])
        self.assertTrue(validate_recipe(
            recipe(est_time_minutes=300, total_time_minutes=300)))

    def test_planner_fits_on_attended_not_active(self) -> None:
        """The bug this whole field exists to prevent."""
        ragu = recipe(est_time_minutes=30, total_time_minutes=150)
        window = 55
        need = attended_minutes(ragu) + config.SLOT_BUFFER_MINUTES
        self.assertGreater(need, window,
                           "a 2-hour braise must not fit a 55-minute window")

    def test_missing_total_falls_back_to_active(self) -> None:
        """Under-booking a slot is safe; over-booking a braise is not."""
        self.assertEqual(attended_minutes({"est_time_minutes": 25}), 25)
        self.assertEqual(attended_minutes(
            {"est_time_minutes": 30, "total_time_minutes": None}), 30)

    def test_attended_handles_empty_row(self) -> None:
        self.assertEqual(attended_minutes({}), 0)


class AdvancePrep(unittest.TestCase):
    def test_overnight_marinade(self) -> None:
        r = recipe(est_time_minutes=20, total_time_minutes=30,
                   advance_prep_minutes=720)
        self.assertEqual(validate_recipe(r), [])
        self.assertEqual(advance_prep(r), 720)

    def test_defaults_to_zero(self) -> None:
        self.assertEqual(advance_prep(recipe()), 0)
        self.assertEqual(advance_prep({}), 0)
        self.assertEqual(advance_prep({"advance_prep_minutes": None}), 0)

    def test_negative_rejected(self) -> None:
        self.assertTrue(validate_recipe(recipe(advance_prep_minutes=-60)))

    def test_absurd_lead_time_rejected(self) -> None:
        problems = validate_recipe(
            recipe(advance_prep_minutes=config.MAX_ADVANCE_PREP_MINUTES + 1))
        self.assertTrue(any("advance_prep_minutes" in p for p in problems))

    def test_unattended_cooking_misfiled_as_advance_prep_is_caught(self) -> None:
        """The dangerous confusion: a braise can't be advance prep, you're home."""
        problems = validate_recipe(recipe(
            est_time_minutes=30,
            total_time_minutes=30,
            advance_prep_minutes=120,
            steps=["Sear the beef.", "Braise for two hours.", "Shred and serve."],
        ))
        self.assertTrue(any("belongs in total_time_minutes" in p for p in problems))

    def test_chilling_is_legitimate_advance_prep(self) -> None:
        """No-bake cheesecake: 20 min work, 4 h chill, and you can go out."""
        r = recipe(
            est_time_minutes=20, total_time_minutes=20, advance_prep_minutes=240,
            steps=["Blitz the biscuits.", "Whip the filling.", "Chill until set."],
        )
        self.assertEqual(validate_recipe(r), [])


class IngredientRules(unittest.TestCase):
    def test_null_quantity_rejected(self) -> None:
        problems = validate_recipe(recipe(
            ingredients=[Ingredient(name="salt", quantity=None, unit="tsp")]))
        self.assertTrue(any("quantity is null" in p for p in problems))

    def test_estimate_must_carry_source_phrasing(self) -> None:
        problems = validate_recipe(recipe(
            ingredients=[Ingredient(name="olive oil", quantity=2, unit="tbsp",
                                    is_approximate=True)]))
        self.assertTrue(any("qualitative_note must" in p for p in problems))

    def test_unlabelled_estimate_rejected(self) -> None:
        problems = validate_recipe(recipe(
            ingredients=[Ingredient(name="olive oil", quantity=2, unit="tbsp",
                                    qualitative_note="a good glug")]))
        self.assertTrue(any("set it true" in p for p in problems))

    def test_duplicates_rejected(self) -> None:
        problems = validate_recipe(recipe(ingredients=[
            Ingredient(name="olive oil", quantity=1, unit="tbsp"),
            Ingredient(name="Olive Oil", quantity=2, unit="tbsp"),
        ]))
        self.assertTrue(any("listed twice" in p for p in problems))

    def test_empty_collections_rejected(self) -> None:
        self.assertTrue(any("steps is empty" in p
                            for p in validate_recipe(recipe(steps=[]))))
        self.assertTrue(any("ingredients is empty" in p
                            for p in validate_recipe(recipe(ingredients=[]))))


class EscalationGate(unittest.TestCase):
    def test_partial_source_escalates(self) -> None:
        self.assertTrue(needs_reconstruction(recipe(source_sufficiency="partial")))

    def test_too_few_ingredients_escalates(self) -> None:
        """Even when the model calls the source complete."""
        self.assertTrue(needs_reconstruction(recipe(
            source_sufficiency="complete",
            ingredients=[Ingredient(name="spinach", quantity=400, unit="g")])))

    def test_no_steps_escalates(self) -> None:
        self.assertTrue(needs_reconstruction(recipe(steps=[])))

    def test_gate_is_deterministic(self) -> None:
        """Same input, same branch, every time - it has to be countable."""
        r = recipe(source_sufficiency="partial")
        self.assertEqual({needs_reconstruction(r) for _ in range(20)}, {True})


class QuantityParsing(unittest.TestCase):
    def test_fractions(self) -> None:
        self.assertEqual(parse_quantity("1/2"), (0.5, False))
        self.assertEqual(parse_quantity("1 1/2"), (1.5, False))
        self.assertEqual(parse_quantity("½"), (0.5, False))
        self.assertEqual(parse_quantity("1½"), (1.5, False))

    def test_ranges_are_approximate(self) -> None:
        self.assertEqual(parse_quantity("2-3"), (2.5, True))
        self.assertEqual(parse_quantity("2 to 3"), (2.5, True))

    def test_vague_counts_are_approximate(self) -> None:
        self.assertEqual(parse_quantity("a few"), (3.0, True))
        self.assertEqual(parse_quantity("a couple"), (2.0, True))

    def test_junk_degrades_to_none(self) -> None:
        for junk in (None, "", "   ", 0, -5, "a good glug"):
            self.assertEqual(parse_quantity(junk)[0], None)


class UnitConversion(unittest.TestCase):
    def test_imperial_rescales_the_number(self) -> None:
        """8 oz must not become '8 unit' - same number, different meaning."""
        self.assertEqual(normalize_measure(8, "oz"), (226.8, "g"))
        self.assertEqual(normalize_measure(1, "lb"), (453.59, "g"))
        self.assertEqual(normalize_measure(1, "stick"), (113.0, "g"))
        self.assertEqual(normalize_measure(2, "fl oz"), (59.15, "ml"))

    def test_canonical_units_unchanged(self) -> None:
        self.assertEqual(normalize_measure(2, "cups"), (2.0, "cup"))
        self.assertEqual(normalize_measure(250, "grams"), (250.0, "g"))

    def test_every_unit_lands_in_the_enum(self) -> None:
        for unit in ("oz", "lb", "stick", "pint", "cups", "smidgen", None, "T", "t"):
            self.assertIn(normalize_measure(1, unit)[1], config.UNITS)


class RowBuilding(unittest.TestCase):
    def test_qualitative_amount_gets_a_usable_number(self) -> None:
        row = to_ingredient_row("Olive oil", None, None,
                                qualitative_note="a good glug")
        self.assertEqual((row["quantity"], row["unit"]), (2.0, "tbsp"))
        self.assertTrue(row["is_approximate"])
        self.assertEqual(render_amount(row), "~2 tbsp (a good glug)")

    def test_exact_amount_renders_without_a_tilde(self) -> None:
        self.assertEqual(render_amount(to_ingredient_row("Flour", 250, "g")), "250 g")

    def test_measure_words_stripped_from_names(self) -> None:
        self.assertEqual(to_ingredient_row("3 cloves garlic")["name"], "garlic")

    def test_duplicates_merge(self) -> None:
        merged = dedupe_ingredients([
            to_ingredient_row("Olive oil", 2, "tbsp"),
            to_ingredient_row("olive oil", 1, "tbsp"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["quantity"], 3.0)

    def test_non_food_identified_but_allowlist_respected(self) -> None:
        self.assertTrue(is_non_food("reserved pasta water"))
        self.assertFalse(is_non_food("coconut water"))


class Provenance(unittest.TestCase):
    """A reconstructed recipe must say so.

    The trap: reconstruction SUCCEEDS, so the final recipe looks complete, so
    asking needs_reconstruction() about it answers "no" — and a recipe rebuilt
    from web research gets labelled as coming from the reel. That inverts the
    flag exactly when it matters, and the calendar then can't warn the cook.
    """

    THIN = ExtractedRecipe(
        title="Mystery Noodles", cuisine="other",
        est_time_minutes=20, total_time_minutes=20, advance_prep_minutes=0,
        servings=2, steps=[],
        ingredients=[Ingredient(name="noodle", quantity=200, unit="g")],
        source_sufficiency="insufficient",
    )
    FULL = ExtractedRecipe(
        title="Gochujang Butter Noodles", cuisine="korean",
        est_time_minutes=20, total_time_minutes=20, advance_prep_minutes=0,
        servings=2, steps=["Boil.", "Toss.", "Serve."],
        ingredients=[
            Ingredient(name="noodle", quantity=200, unit="g"),
            Ingredient(name="gochujang", quantity=2, unit="tbsp"),
            Ingredient(name="butter", quantity=2, unit="tbsp"),
        ],
        source_sufficiency="complete",
    )
    IDENTITY = DishIdentification(
        dish_name="gochujang butter noodles", confident=True,
        reasoning="named in the caption")

    def run_extract(self, structured_results, search_result="Title: x"):
        with patch("stages.extract.call_llm_structured",
                   side_effect=structured_results),              patch("stages.extract.call_llm_with_search",
                   return_value=search_result):
            return _extract_from_source("some caption", "ref")

    def test_reconstruction_is_reported_even_though_it_succeeded(self) -> None:
        recipe, reconstructed, _ = self.run_extract(
            [self.THIN, self.IDENTITY, self.FULL])
        self.assertTrue(reconstructed, "the rebuilt recipe must be labelled")
        self.assertEqual(recipe.title, "Gochujang Butter Noodles")

    def test_original_sufficiency_survives_reconstruction(self) -> None:
        """The rebuilt pass says 'complete'; the SOURCE was 'insufficient'.

        The eval split depends on this: only transcript-sufficient reels can be
        scored against ground truth.
        """
        _, _, sufficiency = self.run_extract(
            [self.THIN, self.IDENTITY, self.FULL])
        self.assertEqual(sufficiency, "insufficient")

    def test_good_source_is_not_reconstructed(self) -> None:
        recipe, reconstructed, sufficiency = self.run_extract([self.FULL])
        self.assertFalse(reconstructed)
        self.assertEqual(sufficiency, "complete")
        self.assertIs(recipe, self.FULL)

    def test_unidentifiable_dish_is_not_reconstructed(self) -> None:
        """Never invent a recipe with no relationship to the reel."""
        unsure = DishIdentification(dish_name=None, confident=False,
                                    reasoning="just a plate of food")
        recipe, reconstructed, _ = self.run_extract([self.THIN, unsure])
        self.assertFalse(reconstructed)
        self.assertIs(recipe, self.THIN)

    def test_failed_search_falls_back_to_the_thin_recipe(self) -> None:
        recipe, reconstructed, _ = self.run_extract(
            [self.THIN, self.IDENTITY], search_result=None)
        self.assertFalse(reconstructed)
        self.assertIs(recipe, self.THIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
