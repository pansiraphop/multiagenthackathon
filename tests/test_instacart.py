"""Offline unit tests for stage 5. No Browserbase or Instacart calls."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import patch

import config
from stages import instacart


class FakePage:
    def __init__(self):
        self.url = "https://www.instacart.com/"
        self.visited: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.visited.append(url)


@contextmanager
def fake_session(page: FakePage):
    yield page


def item(number: int, name: str) -> dict:
    return {"id": f"item-{number}", "ingredient_name": name}


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


if __name__ == "__main__":
    unittest.main()
