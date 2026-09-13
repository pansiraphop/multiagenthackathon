"""Stage 3 tests — scoring and constrained assignment.

Pure functions with fixture data: no network, no database, no model. The
planner is deterministic by design, so these are exact-value assertions rather
than ranges. If one fails, the plan changed.

    python -m tests.test_plan
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import config
from lib.llm import LLMFailure
from lib.schemas import MealReasons
from stages.availability import _at
from stages.plan import (
    _template_reason,
    add_reasons,
    assign,
    base_score,
    completeness,
    expiring_ingredients,
    expiry_urgency,
    pantry_overlap,
    usable_pantry,
)

MONDAY = date(2026, 9, 14)
TODAY = date(2026, 9, 13)
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=config.TIMEZONE)


def pantry(**items) -> dict[str, dict]:
    """pantry(spinach=2, rice=None) -> spinach expires in 2 days, rice is a staple."""
    built = {}
    for name, days in items.items():
        key = name.replace("_", " ")
        built[key] = {
            "ingredient_name": key,
            "quantity": 100,
            "unit": "g",
            "expiry_date": (TODAY + timedelta(days=days)).isoformat()
                           if days is not None else None,
        }
    return built


def recipe(rid="r1", title="Test Dish", cuisine="italian",
           active=30, attended=None, lead=0, names=("spinach",)) -> dict:
    return {
        "id": rid,
        "title": title,
        "cuisine": cuisine,
        "est_time_minutes": active,
        "total_time_minutes": attended if attended is not None else active,
        "advance_prep_minutes": lead,
        "steps": ["Cook it."],
        "ingredients": [{"name": n} for n in names],
    }


def slot(sid="s1", day_offset=0, start="18:00", minutes=120) -> dict:
    day = MONDAY + timedelta(days=day_offset)
    begin = _at(day, start)
    return {
        "id": sid,
        "week_start_date": MONDAY.isoformat(),
        "slot_start": begin.isoformat(),
        "slot_end": (begin + timedelta(minutes=minutes)).isoformat(),
        "duration_minutes": minutes,
        "suitability_score": 1.0,
        "assigned": False,
    }


class ExpiryUrgency(unittest.TestCase):
    def test_nothing_matching_scores_zero(self) -> None:
        self.assertEqual(
            expiry_urgency(recipe(names=("beef",)), pantry(spinach=1), TODAY), 0.0)

    def test_expiring_tomorrow_is_maximally_urgent(self) -> None:
        self.assertEqual(
            expiry_urgency(recipe(names=("spinach",)), pantry(spinach=1), TODAY), 1.0)

    def test_expiring_today_is_maximally_urgent(self) -> None:
        self.assertEqual(
            expiry_urgency(recipe(names=("spinach",)), pantry(spinach=0), TODAY), 1.0)

    def test_a_week_out_contributes_nothing(self) -> None:
        self.assertEqual(
            expiry_urgency(recipe(names=("spinach",)), pantry(spinach=7), TODAY), 0.0)

    def test_mid_range_scales_linearly(self) -> None:
        self.assertAlmostEqual(
            expiry_urgency(recipe(names=("spinach",)), pantry(spinach=4), TODAY),
            0.5, places=4)

    def test_staples_never_count(self) -> None:
        self.assertEqual(
            expiry_urgency(recipe(names=("rice",)), pantry(rice=None), TODAY), 0.0)

    def test_capped_at_one(self) -> None:
        r = recipe(names=("spinach", "paneer", "cream"))
        self.assertEqual(
            expiry_urgency(r, pantry(spinach=1, paneer=1, cream=1), TODAY), 1.0)

    def test_non_food_ignored(self) -> None:
        """Pasta water can't be about to expire."""
        r = recipe(names=("pasta water",))
        self.assertEqual(expiry_urgency(r, pantry(), TODAY), 0.0)


class PantryOverlap(unittest.TestCase):
    def test_everything_on_hand(self) -> None:
        r = recipe(names=("spinach", "rice"))
        self.assertEqual(pantry_overlap(r, pantry(spinach=3, rice=None)), 1.0)

    def test_nothing_on_hand(self) -> None:
        self.assertEqual(pantry_overlap(recipe(names=("beef",)), pantry()), 0.0)

    def test_half_on_hand(self) -> None:
        r = recipe(names=("spinach", "beef"))
        self.assertEqual(pantry_overlap(r, pantry(spinach=3)), 0.5)

    def test_non_food_excluded_from_the_denominator(self) -> None:
        """Water shouldn't dilute the overlap score."""
        r = recipe(names=("spinach", "water"))
        self.assertEqual(pantry_overlap(r, pantry(spinach=3)), 1.0)

    def test_no_ingredients_does_not_divide_by_zero(self) -> None:
        self.assertEqual(pantry_overlap(recipe(names=()), pantry()), 0.0)


class Completeness(unittest.TestCase):
    def test_nothing_missing(self) -> None:
        self.assertEqual(completeness(recipe(names=("spinach",)),
                                      pantry(spinach=3)), 1.0)

    def test_four_missing_is_half(self) -> None:
        r = recipe(names=("a", "b", "c", "d"))
        self.assertEqual(completeness(r, pantry()), 0.5)

    def test_floors_at_zero(self) -> None:
        r = recipe(names=tuple(f"item{n}" for n in range(12)))
        self.assertEqual(completeness(r, pantry()), 0.0)


class BaseScore(unittest.TestCase):
    def test_urgent_and_stocked_beats_neither(self) -> None:
        urgent = recipe(rid="a", names=("spinach",))
        neither = recipe(rid="b", names=("beef", "veal", "quail"))
        store = pantry(spinach=1)
        self.assertGreater(base_score(urgent, store, TODAY),
                           base_score(neither, store, TODAY))

    def test_bounded_zero_to_one(self) -> None:
        store = pantry(spinach=1)
        for r in (recipe(names=("spinach",)), recipe(names=("beef",))):
            score = base_score(r, store, TODAY)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_deterministic(self) -> None:
        r, store = recipe(names=("spinach",)), pantry(spinach=2)
        self.assertEqual(len({base_score(r, store, TODAY) for _ in range(20)}), 1)

    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(
            config.W_EXPIRY + config.W_OVERLAP + config.W_COMPLETENESS, 1.0)


class Assignment(unittest.TestCase):
    def test_fits_on_attended_time_not_active(self) -> None:
        """The braise bug: 30 min active, 150 attended, into a 60-min window."""
        braise = recipe(active=30, attended=150)
        assignments, unschedulable = assign(
            [braise], [slot(minutes=60)], pantry(spinach=1), now=NOW)
        self.assertEqual(assignments, [])
        self.assertEqual([r["id"] for r in unschedulable], ["r1"])

    def test_attended_time_fits_a_long_window(self) -> None:
        braise = recipe(active=30, attended=150)
        assignments, _ = assign(
            [braise], [slot(minutes=240)], pantry(spinach=1), now=NOW)
        self.assertEqual(len(assignments), 1)

    def test_buffer_is_respected(self) -> None:
        """A 50-minute dish does not fit a 60-minute window with a 15-min pad."""
        r = recipe(active=50)
        assignments, _ = assign([r], [slot(minutes=60)], pantry(), now=NOW)
        self.assertEqual(assignments, [])

    def test_block_length_uses_attended_time(self) -> None:
        braise = recipe(active=30, attended=150)
        assignments, _ = assign(
            [braise], [slot(minutes=240)], pantry(), now=NOW)
        start = datetime.fromisoformat(assignments[0]["planned_start_time"])
        end = datetime.fromisoformat(assignments[0]["planned_end_time"])
        self.assertEqual((end - start).total_seconds() / 60, 150)

    def test_urgent_recipe_takes_the_earliest_window(self) -> None:
        urgent = recipe(rid="urgent", cuisine="indian", names=("spinach",))
        relaxed = recipe(rid="relaxed", cuisine="thai", names=("rice",))
        slots = [slot("late", day_offset=3), slot("early", day_offset=0)]
        assignments, _ = assign([relaxed, urgent], slots,
                                pantry(spinach=1, rice=None), now=NOW)
        self.assertEqual(assignments[0]["slot"]["id"], "early")
        self.assertEqual(assignments[0]["recipe"]["id"], "urgent")

    def test_cuisine_diversity_penalised(self) -> None:
        """Two identical Italian dishes: the second is discounted."""
        a = recipe(rid="a", cuisine="italian", names=("spinach",))
        b = recipe(rid="b", cuisine="italian", names=("spinach",))
        assignments, _ = assign([a, b], [slot("s1"), slot("s2", day_offset=1)],
                                pantry(spinach=1), now=NOW)
        self.assertEqual(len(assignments), 2)
        self.assertLess(assignments[1]["score"], assignments[0]["score"])

    def test_advance_prep_blocks_a_too_soon_window(self) -> None:
        """An overnight marinade can't go in a window six hours away."""
        marinade = recipe(lead=720)
        # NOW is Sun 13th 12:00, so this window is 6 hours away - not enough
        # runway for a 12-hour marinade.
        soon = slot("soon", day_offset=-1, start="18:00")
        self.assertLess(
            datetime.fromisoformat(soon["slot_start"]) - NOW, timedelta(hours=12),
            "fixture must actually be too soon")
        assignments, _ = assign([marinade], [soon], pantry(), now=NOW)
        self.assertEqual(assignments, [])

    def test_advance_prep_allows_a_later_window(self) -> None:
        marinade = recipe(lead=720)
        later = slot("later", day_offset=3, start="18:00")
        assignments, _ = assign([marinade], [later], pantry(), now=NOW)
        self.assertEqual(len(assignments), 1)

    def test_respects_meals_per_week(self) -> None:
        recipes = [recipe(rid=f"r{n}", cuisine="other") for n in range(12)]
        slots = [slot(f"s{n}", day_offset=n % 7) for n in range(12)]
        assignments, _ = assign(recipes, slots, pantry(), now=NOW)
        self.assertEqual(len(assignments), config.MEALS_PER_WEEK)

    def test_one_recipe_per_slot_and_one_slot_per_recipe(self) -> None:
        recipes = [recipe(rid=f"r{n}", cuisine="other") for n in range(4)]
        slots = [slot(f"s{n}", day_offset=n) for n in range(4)]
        assignments, _ = assign(recipes, slots, pantry(), now=NOW)
        slot_ids = [a["slot"]["id"] for a in assignments]
        recipe_ids = [a["recipe"]["id"] for a in assignments]
        self.assertEqual(len(slot_ids), len(set(slot_ids)))
        self.assertEqual(len(recipe_ids), len(set(recipe_ids)))

    def test_deterministic_across_runs(self) -> None:
        """Re-running on camera must produce the same plan."""
        recipes = [recipe(rid=f"r{n}", cuisine="other", names=("spinach",))
                   for n in range(5)]
        slots = [slot(f"s{n}", day_offset=n) for n in range(5)]
        plans = {
            tuple((a["recipe"]["id"], a["slot"]["id"])
                  for a in assign(recipes, slots, pantry(spinach=2), now=NOW)[0])
            for _ in range(10)
        }
        self.assertEqual(len(plans), 1)

    def test_ties_broken_deterministically(self) -> None:
        """Identical scores must not depend on input order."""
        a = recipe(rid="aaa", cuisine="other")
        b = recipe(rid="bbb", cuisine="other")
        first, _ = assign([a, b], [slot()], pantry(), now=NOW)
        second, _ = assign([b, a], [slot()], pantry(), now=NOW)
        self.assertEqual(first[0]["recipe"]["id"], second[0]["recipe"]["id"])

    def test_no_slots_plans_nothing(self) -> None:
        self.assertEqual(assign([recipe()], [], pantry(), now=NOW), ([], []))

    def test_no_recipes_plans_nothing(self) -> None:
        assignments, unschedulable = assign([], [slot()], pantry(), now=NOW)
        self.assertEqual((assignments, unschedulable), ([], []))

    def test_unschedulable_only_lists_genuinely_oversized(self) -> None:
        """Left over because the week filled up is not the same as too long."""
        fits = [recipe(rid=f"r{n}", cuisine="other") for n in range(8)]
        assignments, unschedulable = assign(
            fits, [slot(f"s{n}", day_offset=n % 7) for n in range(8)],
            pantry(), now=NOW)
        self.assertEqual(len(assignments), config.MEALS_PER_WEEK)
        self.assertEqual(unschedulable, [],
                         "these fit fine, they just didn't make the cut")


class TemplateReason(unittest.TestCase):
    def item(self, **kw):
        r = recipe(**kw)
        return {
            "recipe": r,
            "slot": slot(),
            "planned_start_time": slot()["slot_start"],
        }

    def test_names_the_expiring_ingredient(self) -> None:
        reason = _template_reason(self.item(names=("spinach",)), pantry(spinach=2))
        self.assertIn("spinach", reason)

    def test_falls_back_when_nothing_is_expiring(self) -> None:
        reason = _template_reason(self.item(names=("rice",)), pantry(rice=None))
        self.assertTrue(reason.strip())
        self.assertNotIn("expiring", reason)

    def test_never_empty(self) -> None:
        for names in (("spinach",), ("rice",), ()):
            self.assertTrue(_template_reason(self.item(names=names),
                                             pantry(spinach=1)).strip())


class ExpiringIngredients(unittest.TestCase):
    def test_sorted_soonest_first(self) -> None:
        r = recipe(names=("cream", "spinach"))
        found = expiring_ingredients(r, pantry(spinach=1, cream=5), TODAY)
        self.assertEqual([name for name, _ in found], ["spinach", "cream"])

    def test_excludes_distant_and_staples(self) -> None:
        r = recipe(names=("spinach", "rice", "flour"))
        found = expiring_ingredients(r, pantry(spinach=2, rice=None, flour=30), TODAY)
        self.assertEqual([name for name, _ in found], ["spinach"])


class UsablePantry(unittest.TestCase):
    """Expired stock is gone, not urgent. Counting it would be the worst of
    both: an inflated urgency score AND the item missing from the cart."""

    ROWS = [
        {"ingredient_name": "spinach", "quantity": 400, "unit": "g",
         "expiry_date": "2026-09-15"},
        {"ingredient_name": "greek yoghurt", "quantity": 200, "unit": "g",
         "expiry_date": "2026-09-10"},                       # expired
        {"ingredient_name": "rice", "quantity": 2, "unit": "kg",
         "expiry_date": None},                                # staple
        {"ingredient_name": "milk", "quantity": 1, "unit": "l",
         "expiry_date": "2026-09-13"},                        # expires today
    ]

    def test_expired_excluded(self) -> None:
        with patch("lib.db.select", return_value=self.ROWS):
            store = usable_pantry(today=TODAY)
        self.assertNotIn("greek yoghurt", store)

    def test_staples_and_future_kept(self) -> None:
        with patch("lib.db.select", return_value=self.ROWS):
            store = usable_pantry(today=TODAY)
        self.assertIn("rice", store)
        self.assertIn("spinach", store)

    def test_expiring_today_is_still_usable(self) -> None:
        """Today's milk is exactly the thing you should cook tonight."""
        with patch("lib.db.select", return_value=self.ROWS):
            store = usable_pantry(today=TODAY)
        self.assertIn("milk", store)

    def test_keyed_by_normalized_name(self) -> None:
        with patch("lib.db.select", return_value=self.ROWS):
            store = usable_pantry(today=TODAY)
        for key in store:
            self.assertEqual(key, key.lower())

    def test_empty_pantry_is_fine(self) -> None:
        with patch("lib.db.select", return_value=[]):
            self.assertEqual(usable_pantry(today=TODAY), {})


class ScoreReasons(unittest.TestCase):
    """score_reason is cosmetic. It must never block or alter a plan."""

    def items(self, count=2):
        built = []
        for n in range(count):
            built.append({
                "recipe": recipe(rid=f"r{n}", title=f"Dish {n}", names=("spinach",)),
                "slot": slot(f"s{n}", day_offset=n),
                "planned_start_time": slot(f"s{n}", day_offset=n)["slot_start"],
            })
        return built

    def test_templates_applied_when_llm_disabled(self) -> None:
        items = self.items()
        add_reasons(items, pantry(spinach=2), use_llm=False)
        for item in items:
            self.assertTrue(item["score_reason"].strip())
            self.assertIn("spinach", item["score_reason"])

    def test_llm_disabled_makes_no_call(self) -> None:
        with patch("stages.plan.call_llm_structured") as call:
            add_reasons(self.items(), pantry(spinach=2), use_llm=False)
        call.assert_not_called()

    def test_one_batched_call_for_the_whole_week(self) -> None:
        """Five meals must be one request, not five."""
        items = self.items(5)
        with patch("stages.plan.call_llm_structured",
                   return_value=MealReasons(reasons=[f"why {n}" for n in range(5)])
                   ) as call:
            add_reasons(items, pantry(spinach=2))
        self.assertEqual(call.call_count, 1)
        self.assertEqual([i["score_reason"] for i in items],
                         [f"why {n}" for n in range(5)])

    def test_model_failure_leaves_templates_intact(self) -> None:
        items = self.items()
        with patch("stages.plan.call_llm_structured",
                   side_effect=LLMFailure("boom")):
            add_reasons(items, pantry(spinach=2))
        for item in items:
            self.assertTrue(item["score_reason"].strip(),
                            "a failed narration must not leave an empty reason")

    def test_blank_reason_falls_back_to_template(self) -> None:
        items = self.items(2)
        with patch("stages.plan.call_llm_structured",
                   return_value=MealReasons(reasons=["", "   "])):
            add_reasons(items, pantry(spinach=2))
        for item in items:
            self.assertTrue(item["score_reason"].strip())

    def test_wrong_count_is_rejected_by_the_validator(self) -> None:
        """Two meals, three reasons: the validator must catch the mismatch."""
        captured = {}

        def fake(prompt, model, **kwargs):
            captured["validate"] = kwargs["validate"]
            return MealReasons(reasons=["a", "b"])

        with patch("stages.plan.call_llm_structured", side_effect=fake):
            add_reasons(self.items(2), pantry(spinach=2))

        validate = captured["validate"]
        self.assertEqual(validate(MealReasons(reasons=["a", "b"])), [])
        self.assertTrue(validate(MealReasons(reasons=["a", "b", "c"])))

    def test_no_assignments_makes_no_call(self) -> None:
        with patch("stages.plan.call_llm_structured") as call:
            add_reasons([], pantry(), use_llm=True)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
