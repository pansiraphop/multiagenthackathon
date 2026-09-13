"""Stage 2 tests — gap inversion, slot scoring, and the fallback path.

Pure functions only: no network, no database, no Google. Every case here is
deterministic, so a failure means the logic changed, not that an API was slow.

    python -m tests.test_availability
"""

from __future__ import annotations

import contextlib
import io as _io
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import config
from stages.availability import (
    _at,
    _fallback_slots,
    _gaps_for_day,
    _merge,
    _row,
    build_slots,
    score_slot,
)

MONDAY = date(2026, 9, 14)
SATURDAY = date(2026, 9, 19)


def gap_minutes(start, end) -> int:
    return int((end - start).total_seconds() // 60)


class MergeIntervals(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_merge([]), [])

    def test_single(self) -> None:
        a, b = _at(MONDAY, "10:00"), _at(MONDAY, "11:00")
        self.assertEqual(_merge([(a, b)]), [(a, b)])

    def test_non_overlapping_preserved_and_sorted(self) -> None:
        early = (_at(MONDAY, "09:00"), _at(MONDAY, "10:00"))
        late = (_at(MONDAY, "14:00"), _at(MONDAY, "15:00"))
        self.assertEqual(_merge([late, early]), [early, late])

    def test_overlapping_merged(self) -> None:
        merged = _merge([
            (_at(MONDAY, "10:00"), _at(MONDAY, "11:00")),
            (_at(MONDAY, "10:30"), _at(MONDAY, "12:00")),
        ])
        self.assertEqual(merged, [(_at(MONDAY, "10:00"), _at(MONDAY, "12:00"))])

    def test_touching_merged(self) -> None:
        """Back-to-back meetings are one busy block, not two."""
        merged = _merge([
            (_at(MONDAY, "10:00"), _at(MONDAY, "11:00")),
            (_at(MONDAY, "11:00"), _at(MONDAY, "12:00")),
        ])
        self.assertEqual(merged, [(_at(MONDAY, "10:00"), _at(MONDAY, "12:00"))])

    def test_fully_nested(self) -> None:
        """An all-day event swallowing a meeting must not shorten the block."""
        merged = _merge([
            (_at(MONDAY, "09:00"), _at(MONDAY, "17:00")),
            (_at(MONDAY, "10:00"), _at(MONDAY, "11:00")),
        ])
        self.assertEqual(merged, [(_at(MONDAY, "09:00"), _at(MONDAY, "17:00"))])


class GapsForDay(unittest.TestCase):
    def test_free_day_is_one_full_window(self) -> None:
        gaps = _gaps_for_day(MONDAY, [])
        self.assertEqual(len(gaps), 1)
        start, end, left, right = gaps[0]
        self.assertEqual(start, _at(MONDAY, config.COOK_WINDOW_START))
        self.assertEqual(end, _at(MONDAY, config.COOK_WINDOW_END))
        self.assertFalse(left, "window edge is not a commitment")
        self.assertFalse(right)

    def test_busy_covering_window_yields_nothing(self) -> None:
        """The Wednesday case: an all-evening offsite means no cooking."""
        busy = [(_at(MONDAY, "16:30"), _at(MONDAY, "22:00"))]
        self.assertEqual(_gaps_for_day(MONDAY, busy), [])

    def test_busy_in_middle_splits_window(self) -> None:
        busy = [(_at(MONDAY, "19:00"), _at(MONDAY, "20:00"))]
        gaps = _gaps_for_day(MONDAY, busy)
        self.assertEqual(len(gaps), 2)

        first, second = gaps
        self.assertEqual(first[0], _at(MONDAY, config.COOK_WINDOW_START))
        self.assertEqual(first[1], _at(MONDAY, "19:00"))
        self.assertFalse(first[2], "starts at the window edge")
        self.assertTrue(first[3], "ends against a commitment")

        self.assertEqual(second[0], _at(MONDAY, "20:00"))
        self.assertEqual(second[1], _at(MONDAY, config.COOK_WINDOW_END))
        self.assertTrue(second[2], "starts after a commitment")
        self.assertFalse(second[3])

    def test_busy_outside_window_ignored(self) -> None:
        """A 9am standup has no bearing on the evening."""
        busy = [(_at(MONDAY, "09:00"), _at(MONDAY, "09:30"))]
        gaps = _gaps_for_day(MONDAY, busy)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gap_minutes(gaps[0][0], gaps[0][1]), 240)

    def test_busy_overlapping_window_start_is_clipped(self) -> None:
        busy = [(_at(MONDAY, "17:00"), _at(MONDAY, "18:15"))]
        gaps = _gaps_for_day(MONDAY, busy)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0], _at(MONDAY, "18:15"))
        self.assertTrue(gaps[0][2], "a commitment abuts the start")

    def test_busy_overlapping_window_end_is_clipped(self) -> None:
        busy = [(_at(MONDAY, "20:00"), _at(MONDAY, "23:00"))]
        gaps = _gaps_for_day(MONDAY, busy)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][1], _at(MONDAY, "20:00"))
        self.assertTrue(gaps[0][3])

    def test_sandwiched_gap_is_flagged_both_sides(self) -> None:
        """Gym then dinner out: the gap between is bounded left AND right."""
        busy = [
            (_at(MONDAY, "18:00"), _at(MONDAY, "19:00")),
            (_at(MONDAY, "19:25"), _at(MONDAY, "21:30")),
        ]
        gaps = _gaps_for_day(MONDAY, busy)
        middle = next(g for g in gaps if g[0] == _at(MONDAY, "19:00"))
        self.assertEqual(gap_minutes(middle[0], middle[1]), 25)
        self.assertTrue(middle[2] and middle[3], "wedged between commitments")

    def test_overlapping_busy_does_not_produce_negative_gap(self) -> None:
        """Double-booked calendars are normal and must not invert a gap."""
        busy = [
            (_at(MONDAY, "18:00"), _at(MONDAY, "20:00")),
            (_at(MONDAY, "19:00"), _at(MONDAY, "19:30")),
        ]
        for start, end, _, _ in _gaps_for_day(MONDAY, busy):
            self.assertLess(start, end)


class ScoreSlot(unittest.TestCase):
    def test_duration_is_capped(self) -> None:
        """Beyond 90 minutes, more time stops earning more score."""
        long_slot = score_slot(_at(MONDAY, "17:30"), _at(MONDAY, "21:30"), False, False)
        medium = score_slot(_at(MONDAY, "17:30"), _at(MONDAY, "19:00"), False, False)
        self.assertEqual(long_slot, medium)

    def test_longer_never_scores_lower(self) -> None:
        base = _at(MONDAY, "17:30")
        scores = [
            score_slot(base, base + timedelta(minutes=m), False, False)
            for m in (30, 60, 90, 150, 240)
        ]
        self.assertEqual(scores, sorted(scores))

    def test_prime_dinner_hour_bonus(self) -> None:
        prime = score_slot(_at(MONDAY, "18:00"), _at(MONDAY, "19:00"), False, False)
        off_peak = score_slot(_at(MONDAY, "20:00"), _at(MONDAY, "21:00"), False, False)
        self.assertGreater(prime, off_peak)

    def test_late_start_penalised(self) -> None:
        late = score_slot(_at(MONDAY, "20:45"), _at(MONDAY, "21:30"), False, False)
        early = score_slot(_at(MONDAY, "17:30"), _at(MONDAY, "18:15"), False, False)
        self.assertLess(late, early)

    def test_sandwiched_short_gap_penalised(self) -> None:
        args = (_at(MONDAY, "19:00"), _at(MONDAY, "19:25"))
        self.assertLess(score_slot(*args, True, True), score_slot(*args, False, False))

    def test_long_gap_not_penalised_for_being_bounded(self) -> None:
        """You can cook in a 2-hour window even if something follows it."""
        args = (_at(MONDAY, "17:30"), _at(MONDAY, "19:30"))
        self.assertEqual(score_slot(*args, True, True), score_slot(*args, False, False))

    def test_weekend_bonus(self) -> None:
        weekend = score_slot(_at(SATURDAY, "17:30"), _at(SATURDAY, "19:00"), False, False)
        weekday = score_slot(_at(MONDAY, "17:30"), _at(MONDAY, "19:00"), False, False)
        self.assertGreater(weekend, weekday)

    def test_never_negative(self) -> None:
        """Worst case: tiny, late, wedged. Still a usable sort key."""
        worst = score_slot(_at(MONDAY, "21:00"), _at(MONDAY, "21:25"), True, True)
        self.assertGreaterEqual(worst, 0.05)


class FallbackSlots(unittest.TestCase):
    def test_one_slot_per_day(self) -> None:
        self.assertEqual(len(_fallback_slots(MONDAY)), 7)

    def test_marked_with_low_score(self) -> None:
        """The low score is the tell that these weren't real windows."""
        for row in _fallback_slots(MONDAY):
            self.assertEqual(row["suitability_score"], 0.1)

    def test_covers_the_whole_week(self) -> None:
        dates = {r["slot_start"][:10] for r in _fallback_slots(MONDAY)}
        expected = {(MONDAY + timedelta(days=n)).isoformat() for n in range(7)}
        self.assertEqual(dates, expected)

    def test_long_enough_to_be_usable(self) -> None:
        for row in _fallback_slots(MONDAY):
            self.assertGreaterEqual(row["duration_minutes"], config.MIN_SLOT_MINUTES)


class RowShape(unittest.TestCase):
    def test_row_fields(self) -> None:
        row = _row(MONDAY, _at(MONDAY, "18:00"), _at(MONDAY, "19:30"), 1.23)
        self.assertEqual(row["week_start_date"], "2026-09-14")
        self.assertEqual(row["duration_minutes"], 90)
        self.assertEqual(row["suitability_score"], 1.23)
        self.assertFalse(row["assigned"])

    def test_timestamps_carry_an_offset(self) -> None:
        """Naive timestamps are the known 2-AM-meals failure mode."""
        row = _row(MONDAY, _at(MONDAY, "18:00"), _at(MONDAY, "19:30"), 1.0)
        self.assertRegex(row["slot_start"], r"[+-]\d{2}:\d{2}$")


@patch("lib.external.log_eval")
@patch("stages.availability.log_eval")
class BuildSlots(unittest.TestCase):
    """build_slots with free/busy mocked out."""

    def setUp(self) -> None:
        # The fallback paths print warnings on purpose. Swallow them so a
        # passing run doesn't look like a failing one.
        self._stdout = contextlib.redirect_stdout(_io.StringIO())
        self._stdout.__enter__()

    def tearDown(self) -> None:
        self._stdout.__exit__(None, None, None)

    BUSY = [
        (_at(MONDAY, "17:00"), _at(MONDAY, "18:15")),          # Mon: 18:15-21:30
        (_at(MONDAY + timedelta(days=2), "16:30"),
         _at(MONDAY + timedelta(days=2), "22:00")),            # Wed: nothing
    ]

    def test_offline_skips_google(self, *_mocks) -> None:
        with patch("stages.availability._query_freebusy") as freebusy:
            rows, real = build_slots(MONDAY, offline=True)
        freebusy.assert_not_called()
        self.assertFalse(real)
        self.assertEqual(len(rows), 7)

    def test_success_path(self, *_mocks) -> None:
        with patch("stages.availability._query_freebusy", return_value=self.BUSY):
            rows, real = build_slots(MONDAY)
        self.assertTrue(real)
        wednesday = (MONDAY + timedelta(days=2)).isoformat()
        self.assertNotIn(wednesday, {r["slot_start"][:10] for r in rows},
                         "a fully booked evening must yield no slot")

    def test_api_failure_falls_back(self, *_mocks) -> None:
        """Availability is upstream of everything, so it must never hard-block."""
        with patch("stages.availability._query_freebusy",
                   side_effect=RuntimeError("403 insufficient scope")):
            rows, real = build_slots(MONDAY)
        self.assertFalse(real)
        self.assertEqual(len(rows), 7)

    def test_fully_booked_week_falls_back(self, *_mocks) -> None:
        all_week = [(_at(MONDAY, "00:00"), _at(MONDAY + timedelta(days=7), "00:00"))]
        with patch("stages.availability._query_freebusy", return_value=all_week):
            rows, real = build_slots(MONDAY)
        self.assertFalse(real, "no windows at all is a degraded result, not a plan")
        self.assertEqual(len(rows), 7)

    def test_short_gaps_filtered(self, *_mocks) -> None:
        busy = [
            (_at(MONDAY, "17:30"), _at(MONDAY, "20:00")),
            (_at(MONDAY, "20:10"), _at(MONDAY, "21:30")),      # only a 10-min gap
        ]
        with patch("stages.availability._query_freebusy", return_value=busy):
            rows, _ = build_slots(MONDAY)
        monday_rows = [r for r in rows if r["slot_start"][:10] == MONDAY.isoformat()]
        self.assertEqual(monday_rows, [], "10 minutes is not a cooking window")

    def test_past_slots_excluded(self, *_mocks) -> None:
        """Running mid-week must not offer windows that already happened."""
        thursday_9pm = _at(MONDAY + timedelta(days=3), "21:00")
        with patch("stages.availability._query_freebusy", return_value=[]), \
             patch("config.now_local", return_value=thursday_9pm):
            rows, _ = build_slots(MONDAY)
        for row in rows:
            self.assertGreater(row["slot_start"], thursday_9pm.isoformat())

    def test_every_row_fits_the_contract(self, *_mocks) -> None:
        with patch("stages.availability._query_freebusy", return_value=self.BUSY):
            rows, _ = build_slots(MONDAY)
        for row in rows:
            self.assertEqual(
                set(row),
                {"week_start_date", "slot_start", "slot_end",
                 "duration_minutes", "suitability_score", "assigned"},
            )
            self.assertGreaterEqual(row["duration_minutes"], config.MIN_SLOT_MINUTES)
            self.assertLess(row["slot_start"], row["slot_end"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
