"""Seed the demo Google Calendar with a realistically busy week.

Ten minutes of work, and the headline feature is invisible without it: on an
empty calendar every window is free, so "InstaCook found the evenings you're
actually free" demonstrates nothing.

The week is designed so the planner has something to show:
  · a long open evening          -> the easy case
  · a 25-minute gap between two commitments -> proves the fit constraint
  · one day with no window at all -> proves it SKIPS days
  · a late-finishing day          -> exercises the late-start penalty
  · an untouched weekend evening  -> the weekend bonus

    python -m seed.seed_calendar           # create the events
    python -m seed.seed_calendar --clear   # remove them again
    python -m seed.seed_calendar --list    # show what's currently seeded
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta

import config
from lib.google_auth import calendar_service

# Every seeded event carries this in its description so --clear can find them
# without touching the user's real events.
MARKER = "[instacook-seed]"

# (day offset from Monday, start, end, title)
BUSY_WEEK = [
    (0, "09:00", "09:30", "Standup"),
    (0, "17:00", "18:15", "1:1 with Priya"),          # leaves 18:15-21:30 open

    (1, "09:00", "09:30", "Standup"),
    (1, "18:00", "19:00", "Gym"),
    (1, "19:25", "21:30", "Dinner with Sam"),          # leaves a 25-min gap

    (2, "09:00", "09:30", "Standup"),
    (2, "16:30", "22:00", "Offsite + team dinner"),    # no window at all

    (3, "09:00", "09:30", "Standup"),
    (3, "20:45", "22:00", "Call with Singapore"),      # late finish

    (4, "09:00", "09:30", "Standup"),
    (4, "15:00", "16:00", "Retro"),                    # evening wide open

    (5, "11:00", "13:00", "Farmers market"),           # Saturday daytime only

    (6, "19:00", "20:00", "Family call"),              # splits Sunday evening
]


def _at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=config.TIMEZONE)


def _seeded_events(service, week_start: date) -> list[dict]:
    window_start = _at(week_start, "00:00")
    result = service.events().list(
        calendarId=config.GOOGLE_CALENDAR_ID,
        timeMin=window_start.isoformat(),
        timeMax=(window_start + timedelta(days=8)).isoformat(),
        singleEvents=True,
        maxResults=250,
    ).execute()
    return [e for e in result.get("items", [])
            if MARKER in (e.get("description") or "")]


def clear(week_start: date) -> int:
    service = calendar_service()
    events = _seeded_events(service, week_start)
    for event in events:
        service.events().delete(
            calendarId=config.GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
    print(f"removed {len(events)} seeded events from the week of {week_start}")
    return len(events)


def seed(week_start: date) -> int:
    service = calendar_service()

    # Idempotent: clear our own seeds first so re-running doesn't double up.
    existing = _seeded_events(service, week_start)
    for event in existing:
        service.events().delete(
            calendarId=config.GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
    if existing:
        print(f"  cleared {len(existing)} previously seeded events")

    created = 0
    for offset, start_hhmm, end_hhmm, title in BUSY_WEEK:
        day = week_start + timedelta(days=offset)
        start, end = _at(day, start_hhmm), _at(day, end_hhmm)
        service.events().insert(
            calendarId=config.GOOGLE_CALENDAR_ID,
            body={
                "summary": title,
                "description": f"{MARKER} demo data for InstaCook",
                "start": {"dateTime": start.isoformat(),
                          "timeZone": str(config.TIMEZONE)},
                "end": {"dateTime": end.isoformat(),
                        "timeZone": str(config.TIMEZONE)},
                "reminders": {"useDefault": False, "overrides": []},
            },
        ).execute()
        print(f"  {start:%a %d %H:%M}-{end:%H:%M}  {title}")
        created += 1

    print(f"seeded {created} events for the week of {week_start}")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD), defaults to upcoming Monday")
    parser.add_argument("--clear", action="store_true", help="remove seeded events")
    parser.add_argument("--list", action="store_true", help="list seeded events")
    args = parser.parse_args()

    week = date.fromisoformat(args.week) if args.week else config.week_start()

    if args.clear:
        clear(week)
    elif args.list:
        for e in _seeded_events(calendar_service(), week):
            print(f"  {e['start'].get('dateTime')}  {e.get('summary')}")
    else:
        seed(week)
