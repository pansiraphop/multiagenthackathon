"""Stage 2 — Google Calendar free/busy -> cook_slots.

Runs BEFORE the planner, which is the whole point: the planner doesn't just
rank recipes, it fits them into windows you actually have. A 45-minute recipe
can't go in a 25-minute gap.

    python -m stages.availability            # the upcoming week
    python -m stages.availability --show     # print slots, don't write
    python -m stages.availability --offline  # skip Google, use fallback slots

Writes rows to cook_slots. Idempotent: deletes the week's slots first.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta

import config
from lib import db
from lib.evals import log_eval
from lib.external import call_external_api

STAGE = "availability"


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def _at(day: date, hhmm: str) -> datetime:
    """Local timezone-aware datetime for a HH:MM on a given day."""
    hour, minute = (int(p) for p in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=config.TIMEZONE)


def _merge(intervals: list[tuple[datetime, datetime]]
           ) -> list[tuple[datetime, datetime]]:
    """Sort and merge overlapping or touching intervals."""
    if not intervals:
        return []
    # Seed from the SORTED list, not the caller's order. Seeding with
    # intervals[0] while iterating the sorted tail silently drops the earliest
    # block when the input isn't already sorted — and a dropped busy block
    # means offering a cook slot in the middle of a meeting.
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
# Google free/busy
# ---------------------------------------------------------------------------

def _query_freebusy(week_start: date) -> list[tuple[datetime, datetime]]:
    """Busy intervals for the week, in local time. Raises on API trouble."""
    from lib.google_auth import calendar_service

    service = calendar_service()
    window_start = _at(week_start, "00:00")
    window_end = window_start + timedelta(days=7)

    response = service.freebusy().query(body={
        "timeMin": window_start.isoformat(),
        "timeMax": window_end.isoformat(),
        "timeZone": str(config.TIMEZONE),
        "items": [{"id": config.GOOGLE_CALENDAR_ID}],
    }).execute()

    entry = response["calendars"][config.GOOGLE_CALENDAR_ID]
    if entry.get("errors"):
        # Most often: the token was granted calendar.events only, so the read
        # half of the scope is missing.
        raise RuntimeError(f"free/busy errors: {entry['errors']}")

    return [
        (datetime.fromisoformat(b["start"]).astimezone(config.TIMEZONE),
         datetime.fromisoformat(b["end"]).astimezone(config.TIMEZONE))
        for b in entry.get("busy", [])
    ]


# ---------------------------------------------------------------------------
# gaps and scoring
# ---------------------------------------------------------------------------

def _gaps_for_day(
    day: date, busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime, bool, bool]]:
    """Invert busy blocks into free gaps inside the day's cooking window.

    Returns (start, end, bounded_left, bounded_right) where the bounded flags
    say whether a real commitment abuts the gap — a short gap wedged between
    two meetings is a bad place to cook, and the score reflects that.
    """
    window_start = _at(day, config.COOK_WINDOW_START)
    window_end = _at(day, config.COOK_WINDOW_END)

    clipped = _merge([
        (max(b_start, window_start), min(b_end, window_end))
        for b_start, b_end in busy
        if b_start < window_end and b_end > window_start
    ])

    gaps: list[tuple[datetime, datetime, bool, bool]] = []
    cursor = window_start
    bounded_left = False

    for b_start, b_end in clipped:
        if b_start > cursor:
            gaps.append((cursor, b_start, bounded_left, True))
        cursor = max(cursor, b_end)
        bounded_left = True

    if cursor < window_end:
        gaps.append((cursor, window_end, bounded_left, False))

    return gaps


def score_slot(
    start: datetime, end: datetime, bounded_left: bool, bounded_right: bool
) -> float:
    """How good a cooking window this is. Tuned by looking at real output."""
    minutes = (end - start).total_seconds() / 60
    local = start.astimezone(config.TIMEZONE)

    score = min(minutes / 60, 1.5)                     # longer is better, capped

    if _at(local.date(), "17:30") <= local < _at(local.date(), "20:00"):
        score += 0.30                                  # prime dinner hour
    if local >= _at(local.date(), "20:30"):
        score -= 0.40                                  # too late to start cooking
    if minutes < 90 and bounded_left and bounded_right:
        score -= 0.50                                  # can't cook between calls
    if local.weekday() >= 5:
        score += 0.20                                  # unhurried weekend cooking

    return round(max(score, 0.05), 3)


def _fallback_slots(week_start: date) -> list[dict]:
    """Default evening slots for when Google is unreachable.

    Availability is upstream of everything, so it must never hard-block the
    pipeline. A degraded plan beats no plan — and the low score makes it
    obvious in the output that these weren't real.
    """
    rows = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        start, end = _at(day, "19:00"), _at(day, "20:15")
        rows.append(_row(week_start, start, end, 0.1))
    return rows


def _row(week_start: date, start: datetime, end: datetime, score: float) -> dict:
    return {
        "week_start_date": week_start.isoformat(),
        "slot_start": start.isoformat(),
        "slot_end": end.isoformat(),
        "duration_minutes": int((end - start).total_seconds() // 60),
        "suitability_score": score,
        "assigned": False,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_slots(week_start: date, offline: bool = False) -> tuple[list[dict], bool]:
    """Return (slot rows, used_real_calendar)."""
    if offline:
        log_eval(STAGE, str(week_start), False,
                 error_message="offline mode: fallback slots")
        return _fallback_slots(week_start), False

    busy, ok = call_external_api(
        _query_freebusy, week_start, stage=STAGE, input_ref=str(week_start))
    if not ok:
        print("  [warn] free/busy failed - falling back to default evening slots")
        return _fallback_slots(week_start), False

    now = config.now_local()
    rows: list[dict] = []

    for offset in range(7):
        day = week_start + timedelta(days=offset)
        for start, end, b_left, b_right in _gaps_for_day(day, busy):
            # Never offer a window that has already started. week_start picks
            # the week; this guarantees the individual hours are still ahead.
            if start <= now:
                continue
            if (end - start).total_seconds() / 60 < config.MIN_SLOT_MINUTES:
                continue
            rows.append(_row(week_start, start, end,
                             score_slot(start, end, b_left, b_right)))

    if not rows:
        print("  [warn] no viable windows found - falling back to default slots")
        return _fallback_slots(week_start), False

    return rows, True


def run(week_start: date | None = None, offline: bool = False,
        show_only: bool = False) -> list[dict]:
    week_start = week_start or config.week_start()
    rows, real = build_slots(week_start, offline=offline)

    source = "calendar" if real else "fallback"
    print(f"[2/6] availability  week of {week_start} | {len(rows)} slots ({source})")
    for r in sorted(rows, key=lambda r: r["slot_start"]):
        start = datetime.fromisoformat(r["slot_start"]).astimezone(config.TIMEZONE)
        print(f"      {start:%a %d %H:%M}  {r['duration_minutes']:>3} min  "
              f"score {r['suitability_score']:.2f}")

    if show_only:
        return rows

    db.delete_where("cook_slots", week_start_date=week_start.isoformat())
    written = db.insert("cook_slots", rows)
    print(f"      wrote {len(written)} cook_slots")
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start date (YYYY-MM-DD), defaults to upcoming Monday")
    parser.add_argument("--offline", action="store_true",
                        help="skip Google and emit fallback slots")
    parser.add_argument("--show", action="store_true",
                        help="print slots without writing to the database")
    args = parser.parse_args()

    run(
        week_start=date.fromisoformat(args.week) if args.week else None,
        offline=args.offline,
        show_only=args.show,
    )
