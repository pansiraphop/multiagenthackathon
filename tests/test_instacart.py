"""Offline unit tests for stage 5. No Browserbase or Instacart calls."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import config
from stages import instacart


class FakePage:
    def __init__(self, fail_cart_navigation: bool = False):
        self.url = "https://www.instacart.com/"
        self.visited: list[str] = []
        self.fail_cart_navigation = fail_cart_navigation

    def goto(self, url: str, **_kwargs) -> None:
        if self.fail_cart_navigation and url.endswith("/store/cart"):
            raise RuntimeError("remote session closed")
        self.url = url
        self.visited.append(url)


@contextmanager
def fake_session(page: FakePage):
    yield page


def item(number: int, name: str, status: str = "pending", unit: str = "unit") -> dict:
    return {
        "id": f"item-{number}",
        "ingredient_name": name,
        "unit": unit,
        "resolution_status": status,
    }


def fake_select(shopping: list[dict], order: dict | None):
    """Stand in for db.select across the three reads stage 5 makes."""
    def select(table: str, columns: str = "*", **_eq):
        if table == "shopping_list":
            return shopping
        if table == "instacart_orders":
            return [order] if order else []
        return []
    return select


class FallbackTests(unittest.TestCase):
    def test_links_are_encoded_and_method_is_recorded(self):
        with patch.object(config, "INSTACART_RETAILER", ""):
            result = instacart._fallback_links([item(1, "red onion & lime")])

        self.assertEqual(result["method"], "fallback_links")
        self.assertEqual(
            result["cart_url"],
            "https://www.instacart.com/store/search/red%20onion%20%26%20lime",
        )
        self.assertEqual(len(result["failed"]), 1)

    def test_retailer_slug_is_used_when_configured(self):
        with patch.object(config, "INSTACART_RETAILER", "local market"):
            url = instacart.search_url("olive oil")
        self.assertIn("/store/local%20market/search/olive%20oil", url)


class BrowserCartTests(unittest.TestCase):
    def test_one_failed_item_does_not_abort_the_batch(self):
        page = FakePage()
        items = [item(1, "tomato"), item(2, "garlic")]

        def add(_page, row):
            if row["ingredient_name"] == "tomato":
                raise TimeoutError("product cards did not load")

        with (
            patch.object(instacart.browser_lib, "session", return_value=fake_session(page)),
            patch.object(instacart, "search_and_add_item", side_effect=add),
            patch.object(instacart.browser_lib, "screenshot") as shot,
            patch.object(instacart, "log_eval"),
            patch.object(config, "BROWSER_ITEM_RETRIES", 0),
        ):
            result = instacart._browser_cart(items)

        self.assertEqual([r["ingredient_name"] for r in result["added"]], ["garlic"])
        self.assertEqual([r["ingredient_name"] for r in result["failed"]], ["tomato"])
        self.assertEqual(result["method"], "browser_automation")
        shot.assert_called_once_with(page, "item-1")
        self.assertEqual(page.url, "https://www.instacart.com/store/cart")

    def test_timeout_is_retried_once_then_succeeds(self):
        page = FakePage()
        attempts = 0

        def flaky(_page, _row):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("transient")

        with (
            patch.object(instacart.browser_lib, "session", return_value=fake_session(page)),
            patch.object(instacart, "search_and_add_item", side_effect=flaky),
            patch.object(instacart.browser_lib, "screenshot") as shot,
            patch.object(instacart, "log_eval") as log,
            patch.object(config, "BROWSER_ITEM_RETRIES", 1),
        ):
            result = instacart._browser_cart([item(1, "tomato")])

        self.assertEqual(attempts, 2)
        self.assertEqual(len(result["added"]), 1)
        shot.assert_not_called()
        self.assertEqual(log.call_args.kwargs["retry_count"], 1)

    def test_final_failure_takes_screenshot(self):
        page = FakePage()
        with (
            patch.object(instacart.browser_lib, "session", return_value=fake_session(page)),
            patch.object(
                instacart,
                "search_and_add_item",
                side_effect=TimeoutError("still unavailable"),
            ),
            patch.object(instacart.browser_lib, "screenshot") as shot,
            patch.object(instacart, "log_eval"),
            patch.object(config, "BROWSER_ITEM_RETRIES", 1),
        ):
            result = instacart._browser_cart([item(8, "spinach")])

        self.assertFalse(result["added"])
        self.assertEqual(len(result["failed"]), 1)
        shot.assert_called_once_with(page, "item-8")

    def test_cart_navigation_failure_preserves_added_items(self):
        page = FakePage(fail_cart_navigation=True)
        with (
            patch.object(instacart.browser_lib, "session", return_value=fake_session(page)),
            patch.object(instacart, "search_and_add_item"),
            patch.object(instacart, "log_eval"),
        ):
            result = instacart._browser_cart([item(1, "tomato")])

        self.assertEqual(len(result["added"]), 1)
        self.assertFalse(result["failed"])
        self.assertEqual(result["cart_url"], "https://www.instacart.com/store/cart")


class DeliveryWindowTests(unittest.TestCase):
    def test_window_ends_before_earliest_meal_and_respects_lead(self):
        now = datetime(2026, 9, 14, 8, 0, tzinfo=config.TIMEZONE)
        plans = [
            {"planned_start_time": "2026-09-14T19:00:00-07:00"},
            {"planned_start_time": "2026-09-15T18:00:00-07:00"},
        ]
        start, end = instacart._delivery_window(
            date(2026, 9, 14), plans=plans, now=now
        )

        self.assertEqual(end, datetime(2026, 9, 14, 17, 0, tzinfo=config.TIMEZONE))
        self.assertEqual(start, datetime(2026, 9, 14, 16, 0, tzinfo=config.TIMEZONE))

    def test_infeasible_window_is_null(self):
        now = datetime(2026, 9, 14, 16, 0, tzinfo=config.TIMEZONE)
        plans = [{"planned_start_time": "2026-09-14T19:00:00-07:00"}]
        self.assertEqual(
            instacart._delivery_window(date(2026, 9, 14), plans=plans, now=now),
            (None, None),
        )


class CartSelectionTests(unittest.TestCase):
    def test_same_ingredient_in_two_units_is_one_product(self):
        rows = [
            item(1, "butter", unit="g"),
            item(2, "butter", unit="tbsp"),
            item(3, "onion"),
        ]
        products, unusable = instacart._cartable(rows)

        self.assertEqual([p["ingredient_name"] for p in products], ["butter", "onion"])
        self.assertEqual(unusable, [])

    def test_blank_ingredient_name_is_refused_not_guessed(self):
        rows = [item(1, "  "), {"id": "x"}, item(2, "onion")]
        products, unusable = instacart._cartable(rows)

        self.assertEqual([p["ingredient_name"] for p in products], ["onion"])
        self.assertEqual(len(unusable), 2)


class OncePerWeekTests(unittest.TestCase):
    def _run(self, shopping, order, **kwargs):
        external = MagicMock(return_value=(
            {
                "cart_url": "https://www.instacart.com/store/cart",
                "links": [],
                "added": [],
                "failed": [],
                "method": "browser_automation",
            },
            True,
        ))
        with (
            patch.object(instacart.db, "select", side_effect=fake_select(shopping, order)),
            patch.object(instacart.db, "delete_where"),
            patch.object(instacart.db, "update_where"),
            patch.object(instacart.db, "insert", return_value=[{
                "method": "browser_automation",
                "item_count": 0,
                "unresolved_item_count": 0,
            }]),
            patch.object(instacart, "_delivery_window", return_value=(None, None)),
            patch.object(instacart, "log_eval"),
            patch.object(instacart, "call_external_api", external),
        ):
            result = instacart.run(date(2026, 9, 14), **kwargs)
        return result, external

    def test_completed_week_is_never_ordered_twice(self):
        order = {"method": "browser_automation", "item_count": 2,
                 "unresolved_item_count": 0}
        shopping = [item(1, "tomato", "added_to_cart"),
                    item(2, "garlic", "added_to_cart")]

        result, external = self._run(shopping, order)

        self.assertEqual(result, order)
        external.assert_not_called()

    def test_partial_week_resumes_only_the_missing_items(self):
        order = {"method": "browser_automation", "item_count": 1,
                 "unresolved_item_count": 1}
        shopping = [item(1, "tomato", "added_to_cart"), item(2, "garlic")]

        _result, external = self._run(shopping, order)

        external.assert_called_once()
        sent = external.call_args.args[1]
        self.assertEqual([r["ingredient_name"] for r in sent], ["garlic"])

    def test_links_only_week_retries_every_item(self):
        order = {"method": "fallback_links", "item_count": 0,
                 "unresolved_item_count": 2}
        shopping = [item(1, "tomato", "fallback_link"),
                    item(2, "garlic", "fallback_link")]

        _result, external = self._run(shopping, order)

        sent = external.call_args.args[1]
        self.assertEqual([r["ingredient_name"] for r in sent], ["tomato", "garlic"])

    def test_force_reorders_a_completed_week(self):
        order = {"method": "browser_automation", "item_count": 2,
                 "unresolved_item_count": 0}
        shopping = [item(1, "tomato", "added_to_cart"),
                    item(2, "garlic", "added_to_cart")]

        _result, external = self._run(shopping, order, force=True)

        sent = external.call_args.args[1]
        self.assertEqual(len(sent), 2)

    def test_dry_run_touches_nothing(self):
        shopping = [item(1, "tomato")]
        with (
            patch.object(instacart.db, "select", side_effect=fake_select(shopping, None)),
            patch.object(instacart.db, "insert") as insert,
            patch.object(instacart.db, "delete_where") as delete,
            patch.object(instacart.db, "update_where") as update,
            patch.object(instacart, "call_external_api") as external,
            patch.object(instacart, "log_eval"),
        ):
            result = instacart.run(date(2026, 9, 14), dry_run=True)

        self.assertEqual(result["method"], "dry_run")
        external.assert_not_called()
        insert.assert_not_called()
        delete.assert_not_called()
        update.assert_not_called()


class WriteTests(unittest.TestCase):
    def _write(self, result, shopping):
        inserted = {"id": "order-1"}
        with (
            patch.object(instacart, "_delivery_window", return_value=(None, None)),
            patch.object(instacart, "log_eval"),
            patch.object(instacart.db, "select", side_effect=fake_select(shopping, None)),
            patch.object(instacart.db, "delete_where"),
            patch.object(instacart.db, "insert", return_value=[inserted]) as insert,
            patch.object(instacart.db, "update_where") as update,
        ):
            instacart._write_order(date(2026, 9, 14), result)
        return insert.call_args.args[1], update

    def test_marks_by_name_so_one_product_covers_both_unit_rows(self):
        result = {
            "cart_url": "https://www.instacart.com/store/cart",
            "added": [item(1, "butter", unit="g")],
            "failed": [],
            "method": "browser_automation",
        }
        shopping = [item(1, "butter", "added_to_cart", unit="g"),
                    item(2, "butter", "added_to_cart", unit="tbsp")]

        row, update = self._write(result, shopping)

        self.assertEqual(
            update.call_args_list[0].args,
            ("shopping_list", {"resolution_status": "added_to_cart"}),
        )
        self.assertEqual(
            update.call_args_list[0].kwargs,
            {"week_start_date": "2026-09-14", "ingredient_name": "butter"},
        )
        self.assertEqual(row["item_count"], 2)
        self.assertEqual(row["unresolved_item_count"], 0)

    def test_counts_come_from_the_week_not_from_this_run(self):
        result = {
            "cart_url": "https://www.instacart.com/store/cart",
            "added": [item(9, "turmeric")],
            "failed": [],
            "method": "browser_automation",
        }
        shopping = [item(i, f"ing{i}", "added_to_cart") for i in range(26)]
        shopping.append(item(9, "turmeric", "added_to_cart"))
        shopping.append(item(99, "worcestershire sauce"))

        row, _update = self._write(result, shopping)

        self.assertEqual(row["item_count"], 27)
        self.assertEqual(row["unresolved_item_count"], 1)

    def test_failed_topup_on_a_real_cart_is_reported_as_mixed(self):
        result = {
            "cart_url": "https://www.instacart.com/store/search/turmeric",
            "added": [],
            "failed": [item(9, "turmeric")],
            "method": "fallback_links",
        }
        shopping = [item(1, "tomato", "added_to_cart"),
                    item(9, "turmeric", "fallback_link")]

        row, _update = self._write(result, shopping)

        self.assertEqual(row["method"], "mixed")
        self.assertEqual(row["cart_url"], "https://www.instacart.com/store/cart")
        self.assertEqual(row["item_count"], 1)


class StatusVocabulary(unittest.TestCase):
    """Stage 5 writes these statuses; stage 5b reads them to find what to ask about.

    A rename on one side and not the other wouldn't crash — the follow-up loop
    would just silently stop noticing that ingredients are missing. So the
    vocabulary is asserted rather than assumed.
    """

    def statuses_written(self, method):
        result = {
            "cart_url": "u",
            "added": [item(1, "tomato")],
            "failed": [item(2, "garlic")],
            "method": method,
        }
        with (
            patch.object(instacart, "_delivery_window", return_value=(None, None)),
            patch.object(instacart, "log_eval"),
            patch.object(instacart.db, "select", side_effect=fake_select([], None)),
            patch.object(instacart.db, "delete_where"),
            patch.object(instacart.db, "insert", return_value=[{}]),
            patch.object(instacart.db, "update_where") as update,
        ):
            instacart._write_order(date(2026, 9, 14), result)
        return [c.args[1]["resolution_status"] for c in update.call_args_list]

    def test_browser_failures_read_as_unresolved_to_the_follow_up_loop(self):
        added, failed = self.statuses_written("browser_automation")
        self.assertEqual(added, config.CART_READY_STATUS)
        self.assertIn(failed, config.UNRESOLVED_STATUSES)

    def test_fallback_links_read_as_unresolved_to_the_follow_up_loop(self):
        _added, failed = self.statuses_written("fallback_links")
        self.assertIn(failed, config.UNRESOLVED_STATUSES)

    def test_cart_ready_is_never_treated_as_unresolved(self):
        self.assertNotIn(config.CART_READY_STATUS, config.UNRESOLVED_STATUSES)


if __name__ == "__main__":
    unittest.main()
