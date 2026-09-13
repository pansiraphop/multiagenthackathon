"""Stage 6 tests — event bodies, and the feedback edge back to stage 3.

Nothing here touches Google or Instacart: event construction is pure, and the
conflict check is exercised against mocked database rows. Stage 5 is still
being built, so these must pass whether or not an instacart_orders row exists.

    python -m tests.test_calendar_sync
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import config
from stages.calendar_sync import (
    MARKER,
    build_delivery_event,
    build_description,
    build_event,
)
from stages.plan import delivery_conflict

MONDAY_6PM = datetime(2026, 9, 14, 18, 0, tzinfo=config.TIMEZONE)


def meal(**over) -> dict:
    base = {
        "id": "meal-1",
        "recipe_id": "r1",
        "cook_slot_id": "slot-1",
        "week_start_date": "2026-09-14",
        "planned_start_time": MONDAY_6PM.isoformat(),
        "planned_end_time": (MONDAY_6PM + timedelta(minutes=35)).isoformat(),
        "score": 0.95,
        "score_reason": "Uses the spinach expiring Wednesday.",
        "calendar_event_id": None,
        "status": "planned",
    }
    base.update(over)
    return base


def recipe(**over) -> dict:
    base = {
        "id": "r1",
        "title": "Weeknight Palak Paneer",
        "cuisine": "indian",
        "est_time_minutes": 35,
        "total_time_minutes": 35,
        "advance_prep_minutes": 0,
        "servings": 4,
        "steps": ["Blanch the spinach.", "Fry the paneer."],
        "provenance": "transcript",
        "source_url": "https://instagram.com/reel/abc",
        "ingredients": [
            {"name": "spinach", "quantity": 400, "unit": "g",
             "is_approximate": False, "qualitative_note": None},
            {"name": "ghee", "quantity": 2, "unit": "tbsp",
             "is_approximate": True, "qualitative_note": "a good glug"},
        ],
    }
    base.update(over)
    return base


def order(**over) -> dict:
    base = {
        "id": "order-1",
        "week_start_date": "2026-09-14",
        "cart_url": "https://instacart.com/cart/xyz",
        "item_count": 12,
        "unresolved_item_count": 0,
        "delivery_window_start": (MONDAY_6PM - timedelta(hours=8)).isoformat(),
        "delivery_window_end": (MONDAY_6PM - timedelta(hours=6)).isoformat(),
        "delivery_event_id": None,
        "method": "browserbase",
    }
    base.update(over)
    return base


class EventBody(unittest.TestCase):
    def test_times_carry_an_explicit_timezone(self) -> None:
        """Ground rule 2. A naive insert puts every meal at 2 AM."""
        body = build_event(meal(), recipe(), order())
        self.assertEqual(body["start"]["timeZone"], str(config.TIMEZONE))
        self.assertEqual(body["end"]["timeZone"], str(config.TIMEZONE))

    def test_block_spans_the_planned_times(self) -> None:
        body = build_event(meal(), recipe(), order())
        self.assertEqual(body["start"]["dateTime"], MONDAY_6PM.isoformat())
        self.assertEqual(
            body["end"]["dateTime"],
            (MONDAY_6PM + timedelta(minutes=35)).isoformat())

    def test_title_is_the_recipe(self) -> None:
        self.assertEqual(build_event(meal(), recipe(), None)["summary"],
                         "Weeknight Palak Paneer")

    def test_untitled_recipe_still_produces_an_event(self) -> None:
        body = build_event(meal(), recipe(title=None), None)
        self.assertTrue(body["summary"].strip())

    def test_marked_so_clear_can_find_it(self) -> None:
        self.assertIn(MARKER, build_event(meal(), recipe(), None)["description"])


class Description(unittest.TestCase):
    def body(self, m=None, r=None, o=None) -> str:
        return build_description(m or meal(), r or recipe(), o)

    def test_leads_with_the_reason(self) -> None:
        self.assertTrue(self.body().startswith("Uses the spinach expiring"))

    def test_estimates_are_visibly_estimates(self) -> None:
        """A cook must be able to tell a guess from a measurement."""
        self.assertIn("~2 tbsp (a good glug)", self.body())
        self.assertIn("400 g", self.body())

    def test_steps_are_numbered(self) -> None:
        self.assertIn("1. Blanch the spinach.", self.body())

    def test_reconstructed_recipes_say_so(self) -> None:
        text = self.body(r=recipe(provenance="reconstructed"))
        self.assertIn("reconstructed", text.lower())

    def test_transcript_recipes_carry_no_warning(self) -> None:
        self.assertNotIn("reconstructed", self.body().lower())

    def test_advance_prep_gives_a_start_by_time(self) -> None:
        """12h before a 6pm cook is 6am the same morning."""
        text = self.body(r=recipe(advance_prep_minutes=720))
        self.assertIn("START AHEAD", text)
        self.assertIn("12h", text)
        self.assertIn("Mon 14 Sep, 06:00", text)

    def test_long_advance_prep_crosses_the_day_boundary(self) -> None:
        """An 8-hour rise plus overnight means the day BEFORE, and it must say so."""
        text = self.body(r=recipe(advance_prep_minutes=24 * 60))
        self.assertIn("Sun 13 Sep, 18:00", text)

    def test_no_advance_prep_says_nothing(self) -> None:
        self.assertNotIn("START AHEAD", self.body())

    def test_attended_time_is_shown_when_it_differs(self) -> None:
        text = self.body(r=recipe(est_time_minutes=30, total_time_minutes=150))
        self.assertIn("30 min hands-on", text)
        self.assertIn("150 min total", text)

    def test_quick_dish_shows_one_time(self) -> None:
        self.assertNotIn("hands-on", self.body())

    def test_cart_link_included_when_present(self) -> None:
        self.assertIn("https://instacart.com/cart/xyz", self.body(o=order()))

    def test_unresolved_items_are_flagged(self) -> None:
        text = self.body(o=order(unresolved_item_count=3))
        self.assertIn("3 item(s) need picking by hand", text)

    def test_fallback_tier_is_not_called_a_cart(self) -> None:
        """The floor tier is a search link. Calling it a cart is a lie the cook
        only discovers standing in the shop."""
        text = self.body(o=order(method="fallback_links", item_count=0,
                                 unresolved_item_count=28))
        self.assertIn("search link", text)
        self.assertIn("28 to find", text)

    def test_real_cart_is_presented_as_one(self) -> None:
        text = self.body(o=order(method="browserbase"))
        self.assertNotIn("search link", text)

    def test_mixed_tier_is_a_real_cart_with_leftovers_flagged(self) -> None:
        """A resumed top-up that failed: 26 in the cart, 2 to find by hand."""
        text = self.body(o=order(method="mixed", item_count=26,
                                 unresolved_item_count=2))
        self.assertNotIn("search link", text)
        self.assertIn("2 item(s) need picking by hand", text)

    def test_no_order_degrades_gracefully(self) -> None:
        """Stage 5 is unfinished. The meal event must still be useful."""
        text = self.body(o=None)
        self.assertIn("shopping list not built yet", text)
        self.assertIn("400 g", text, "the recipe is still complete")

    def test_cook_session_link_always_present(self) -> None:
        """Shipped from the first version so stage 7 needs no rework."""
        self.assertIn(f"{config.APP_BASE_URL}/cook/meal-1", self.body())

    def test_missing_steps_do_not_break_the_body(self) -> None:
        text = self.body(r=recipe(steps=[]))
        self.assertIn("no method recorded", text)

    def test_missing_ingredients_do_not_break_the_body(self) -> None:
        text = self.body(r=recipe(ingredients=[]))
        self.assertIn("none recorded", text)

    def test_null_quantity_renders_without_crashing(self) -> None:
        r = recipe(ingredients=[{"name": "salt", "quantity": None, "unit": "tsp",
                                 "is_approximate": True,
                                 "qualitative_note": "to taste"}])
        self.assertIn("to taste", self.body(r=r))


class DeliveryEvent(unittest.TestCase):
    def test_built_when_a_window_exists(self) -> None:
        event = build_delivery_event(order(), [meal(_title="Palak Paneer")])
        self.assertIsNotNone(event)
        self.assertEqual(event["start"]["timeZone"], str(config.TIMEZONE))
        self.assertIn("12 items", event["summary"])

    def test_names_the_meals_it_unblocks(self) -> None:
        event = build_delivery_event(order(), [meal(_title="Palak Paneer")])
        self.assertIn("Palak Paneer", event["description"])

    def test_fallback_tier_reports_the_real_count(self) -> None:
        """item_count is 0 on the fallback tier; '0 items arriving' is wrong."""
        event = build_delivery_event(
            order(method="fallback_links", item_count=0,
                  unresolved_item_count=28), [])
        self.assertIn("28 item(s)", event["description"])
        self.assertIn("28 items", event["summary"])
        self.assertIn("no cart was built", event["description"])

    def test_skipped_without_a_window(self) -> None:
        """Not every Instacart tier can set a delivery window."""
        self.assertIsNone(
            build_delivery_event(order(delivery_window_start=None), []))
        self.assertIsNone(
            build_delivery_event(order(delivery_window_end=None), []))


class DeliveryConflict(unittest.TestCase):
    """The feedback edge. Stage 5's answer can invalidate stage 3's plan."""

    def rows(self, orders, meals):
        def fake_select(table, _cols="*", **_eq):
            return {"instacart_orders": orders, "meal_plan": meals}.get(table, [])
        return fake_select

    def check(self, orders, meals, recipe_row=None):
        with patch("lib.db.select", side_effect=self.rows(orders, meals)), \
             patch("lib.db.get_recipe", return_value=recipe_row or recipe()):
            return delivery_conflict(config.week_start())

    def test_no_order_means_no_conflict(self) -> None:
        """Stage 5 hasn't run. That is not a conflict."""
        self.assertIsNone(self.check([], [meal()]))

    def test_no_meals_means_no_conflict(self) -> None:
        self.assertIsNone(self.check([order()], []))

    def test_advisory_window_means_no_conflict(self) -> None:
        """Some Instacart tiers can't set a window at all."""
        self.assertIsNone(self.check([order(delivery_window_end=None)], [meal()]))

    def test_comfortable_delivery_is_feasible(self) -> None:
        self.assertIsNone(self.check([order()], [meal()]))

    def test_late_delivery_is_a_conflict(self) -> None:
        late = order(
            delivery_window_end=(MONDAY_6PM - timedelta(minutes=30)).isoformat())
        conflict = self.check([late], [meal()])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["slot_id"], "slot-1")
        self.assertGreater(conflict["short_by_minutes"], 0)

    def test_exactly_on_the_buffer_is_feasible(self) -> None:
        """The boundary must not flap between runs."""
        edge = order(delivery_window_end=(
            MONDAY_6PM - timedelta(hours=config.DELIVERY_BUFFER_HRS)).isoformat())
        self.assertIsNone(self.check([edge], [meal()]))

    def test_conflict_targets_the_EARLIEST_meal(self) -> None:
        """Only the first slot is unusable; later meals are fine."""
        later = meal(id="meal-2", cook_slot_id="slot-2",
                     planned_start_time=(MONDAY_6PM + timedelta(days=2)).isoformat())
        late = order(
            delivery_window_end=(MONDAY_6PM - timedelta(minutes=30)).isoformat())
        conflict = self.check([late], [later, meal()])
        self.assertEqual(conflict["slot_id"], "slot-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
