"""Stage 5b — when the world says no, ask the person who saved the reels.

Two things can invalidate a finished plan, and neither is a bug:

  * the groceries cannot land before the earliest cook slot, so that evening is
    not really available after all;
  * the cart could not resolve some ingredients, so a meal is short of what it
    needs.

Both used to be resolved silently — re-plan around it and move on. That is a
reasonable *fallback* but a poor *first move*, because only the user knows
whether they'd rather move Monday's dinner, cook something else that night, or
pick up two things themselves. So the agent asks, over the same Instagram thread
the reels arrived in, and offers concrete options: reschedule, or swap in one of
their own previously-sent reels that fits the same window.

    python -m stages.followup                     # detect and ask
    python -m stages.followup --dry-run           # decide and print, write nothing
    python -m stages.followup --reply "2"         # act as if the user replied 2
    python -m stages.followup --status            # show this week's conversation

The loop is bounded by `FOLLOWUP_MAX_ROUNDS`: each round is one question, one
answer, and one re-plan. After the bound the agent stops asking, resolves the
week deterministically, and says so — a loop that can't terminate is worse than
no loop.

This is the one stage besides run_week.py that re-runs earlier stages. That is
what a feedback edge is: applying an answer means re-planning, rebuilding the
shopping list, and re-syncing the calendar.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from typing import Any

import config
from lib import db, instagram
from lib.evals import log_eval
from lib.schemas import advance_prep, attended_minutes
from stages import calendar_sync, plan, shopping_list

STAGE = "followup"

ISSUE_DELIVERY = "delivery_conflict"
ISSUE_MISSING = "missing_ingredients"
OPEN_STATUS = "sent"


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def _unresolved_items(week: str) -> list[dict]:
    return [
        row for row in db.select("shopping_list", "*", week_start_date=week)
        if row["resolution_status"] in config.UNRESOLVED_STATUSES
    ]


def _meal_titles(week: str) -> dict[str, dict]:
    """meal_plan rows for the week, each with its recipe attached."""
    meals = {}
    for meal in db.select("meal_plan", "*", week_start_date=week):
        recipe = db.get_recipe_with_ingredients(meal["recipe_id"]) or {}
        meals[meal["id"]] = dict(meal, _recipe=recipe)
    return meals


def _worst_affected_meal(week: str, missing: set[str]) -> dict | None:
    """The planned meal that loses the most to the unresolved ingredients.

    Ties break on the earliest meal, because that is the one whose decision is
    most urgent.
    """
    scored = []
    for meal in _meal_titles(week).values():
        needed = {i["name"] for i in meal["_recipe"].get("ingredients") or []}
        overlap = needed & missing
        if overlap:
            scored.append((-len(overlap), meal["planned_start_time"], meal))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]))
    return scored[0][2]


def open_issues(week_start: date) -> list[dict]:
    """Everything about this week that needs a human decision, worst first.

    Reads rows only, so it never depends on stage 5 having run in this process.
    """
    week = week_start.isoformat()
    issues: list[dict] = []

    conflict = plan.delivery_conflict(week_start)
    if conflict:
        issues.append({
            "kind": ISSUE_DELIVERY,
            "meal_id": conflict["meal_id"],
            "slot_id": conflict["slot_id"],
            "title": conflict["title"],
            "detail": (
                f"the groceries don't land until "
                f"{conflict['delivery_end'].astimezone(config.TIMEZONE):%a %H:%M}, "
                f"which is {conflict['short_by_minutes']} minutes too late for "
                f"{conflict['title']} at "
                f"{conflict['planned_start'].astimezone(config.TIMEZONE):%a %H:%M}"
            ),
            "avoid": set(),
        })

    unresolved = _unresolved_items(week)
    if unresolved:
        missing = {row["ingredient_name"] for row in unresolved}
        meal = _worst_affected_meal(week, missing)
        if meal:
            short = sorted(
                {i["name"] for i in meal["_recipe"].get("ingredients") or []} & missing
            )
            issues.append({
                "kind": ISSUE_MISSING,
                "meal_id": meal["id"],
                "slot_id": meal.get("cook_slot_id"),
                "title": meal["_recipe"].get("title") or meal["recipe_id"],
                "detail": (
                    f"I couldn't get {_join(short)} into the cart, and "
                    f"{meal['_recipe'].get('title')} needs "
                    f"{'them' if len(short) > 1 else 'it'}"
                ),
                "avoid": missing,
            })
    return issues


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else "some items"
    return f"{', '.join(names[:-1])} and {names[-1]}"


# ---------------------------------------------------------------------------
# options — the swap list is the user's own earlier reels
# ---------------------------------------------------------------------------

def swap_candidates(
    week_start: date,
    *,
    slot: dict | None,
    avoid: set[str],
    now: datetime | None = None,
) -> list[dict]:
    """Previously-sent reels that could take this slot instead, best first.

    Ranked by how little new shopping they imply: a candidate needing an
    ingredient the cart already failed on is no use, so those sort last.
    """
    week = week_start.isoformat()
    planned = {m["recipe_id"] for m in db.select("meal_plan", "recipe_id",
                                                 week_start_date=week)}
    pantry = plan.usable_pantry()
    now = now or config.now_local()

    ranked = []
    for recipe in db.successful_recipes():
        if recipe["id"] in planned:
            continue
        if slot:
            capacity = slot["duration_minutes"] - config.SLOT_BUFFER_MINUTES
            if attended_minutes(recipe) > capacity:
                continue
            lead = advance_prep(recipe)
            slot_start = datetime.fromisoformat(slot["slot_start"])
            if lead and now + timedelta(minutes=lead) > slot_start:
                continue
        names = {i["name"] for i in recipe.get("ingredients") or []}
        ranked.append((len(names & avoid), -plan.base_score(recipe, pantry),
                       recipe["id"], recipe))
    ranked.sort(key=lambda row: row[:3])
    return [row[3] for row in ranked[:config.FOLLOWUP_SWAP_OPTIONS]]


def build_options(issue: dict, candidates: list[dict]) -> list[dict]:
    """Numbered options. `action` is what apply_reply() dispatches on."""
    options: list[dict] = []

    def add(action: str, label: str, **extra: Any) -> None:
        options.append(dict(key=str(len(options) + 1), action=action,
                            label=label, **extra))

    if issue["kind"] == ISSUE_DELIVERY:
        add("later", f"Move {issue['title']} later this week",
            slot_id=issue["slot_id"])
        for recipe in candidates:
            add("swap", f"Cook {recipe['title']} that night instead",
                recipe_id=recipe["id"], meal_id=issue["meal_id"])
        add("keep", "Keep it and I'll shop for the rest myself")
    else:
        add("keep", "Cook it without them")
        for recipe in candidates:
            add("swap", f"Cook {recipe['title']} instead",
                recipe_id=recipe["id"], meal_id=issue["meal_id"])
        add("later", f"Move {issue['title']} later so I can buy them",
            slot_id=issue["slot_id"])
    return options


def compose(issue: dict, options: list[dict], *, final_round: bool) -> str:
    lines = [f"Heads up - {issue['detail']}.", ""]
    lines.append("Reply with a number:")
    for option in options:
        lines.append(f"{option['key']}. {option['label']}")
    if final_round:
        lines += ["", "This is my last check-in for this week - if I don't hear "
                      "back I'll go with option 1."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# asking
# ---------------------------------------------------------------------------

def _open_followups(week: str) -> list[dict]:
    return db.select("followups", "*", week_start_date=week, status=OPEN_STATUS)


def ask(
    week_start: date,
    issue: dict,
    *,
    round_number: int = 1,
    dry_run: bool = False,
) -> dict | None:
    """Send one question and record it. Returns the followups row, or None."""
    week = week_start.isoformat()
    slot = None
    if issue.get("slot_id"):
        rows = db.select("cook_slots", "*", id=issue["slot_id"])
        slot = rows[0] if rows else None

    candidates = swap_candidates(week_start, slot=slot, avoid=issue["avoid"])
    options = build_options(issue, candidates)
    final = round_number >= config.FOLLOWUP_MAX_ROUNDS
    question = compose(issue, options, final_round=final)
    recipient = db.latest_sender_id()

    if dry_run:
        print(f"      would ask ({issue['kind']}, round {round_number}, "
              f"recipient {recipient or 'unknown'}):")
        for line in question.splitlines():
            print(f"      | {line}")
        return None

    delivery = instagram.send_dm(recipient, question, options)

    # A new question makes any earlier one for this week stale. Answering a
    # superseded question would apply a decision about a plan that no longer
    # exists.
    for row in _open_followups(week):
        db.update("followups", row["id"], {"status": "superseded"})

    written = db.insert("followups", {
        "week_start_date": week,
        "kind": issue["kind"],
        "meal_plan_id": issue.get("meal_id"),
        "recipient_id": recipient,
        "channel": delivery.channel,
        "question": question,
        "options": options,
        "status": OPEN_STATUS if delivery.delivered else "unreachable",
        "provider_message_id": delivery.message_id,
        "round": round_number,
    })[0]

    log_eval(STAGE, f"{week}:{issue['kind']}:ask", delivery.delivered,
             error_message=delivery.error)
    if delivery.delivered:
        print(f"      asked the user over Instagram DM ({len(options)} options, "
              f"round {round_number}) - waiting for a reply")
    return written


# ---------------------------------------------------------------------------
# applying an answer
# ---------------------------------------------------------------------------

def _rebuild(week_start: date, *, use_llm: bool,
             exclude_slots: tuple[str, ...] = ()) -> None:
    plan.run(week_start=week_start, use_llm=use_llm, exclude_slots=exclude_slots)
    shopping_list.run(week_start=week_start)


def _swap_meal(week_start: date, option: dict) -> str:
    """Put a different recipe in the same window, leaving the rest of the week alone.

    A targeted edit rather than a re-plan: the user chose this dish for this
    evening, so re-scoring the whole week could move it straight back out.
    """
    week = week_start.isoformat()
    rows = db.select("meal_plan", "*", id=option["meal_id"])
    if not rows:
        return "that meal is no longer in the plan, so there was nothing to swap"
    meal = rows[0]
    recipe = db.get_recipe_with_ingredients(option["recipe_id"])
    if not recipe:
        return "that recipe is no longer available"

    start = datetime.fromisoformat(meal["planned_start_time"])
    db.update("meal_plan", meal["id"], {
        "recipe_id": recipe["id"],
        # The window is the user's choice; only the block length changes.
        "planned_end_time": (
            start + timedelta(minutes=attended_minutes(recipe))
        ).isoformat(),
        "score_reason": f"You chose this over the original plan for "
                        f"{start.astimezone(config.TIMEZONE):%A}.",
        "status": "planned",
    })
    shopping_list.run(week_start=week_start)
    return f"swapped in {recipe['title']} for {start.astimezone(config.TIMEZONE):%A}"


def apply_option(
    week_start: date,
    option: dict,
    *,
    use_llm: bool = True,
) -> str:
    """Carry out one chosen option. Returns a one-line resolution for the record."""
    action = option.get("action")
    if action == "later":
        slot_id = option.get("slot_id")
        _rebuild(week_start, use_llm=use_llm,
                 exclude_slots=(slot_id,) if slot_id else ())
        return "re-planned the week without that window"
    if action == "swap":
        return _swap_meal(week_start, option)
    return "left the plan as it is"


def apply_reply(
    reply: instagram.TextReply,
    *,
    week_start: date | None = None,
    use_llm: bool = True,
) -> dict | None:
    """Match an inbound DM to the open question and act on it.

    Safe to call for any inbound message: a reply that matches nothing gets a
    short clarification instead of a guessed action.
    """
    week_start = week_start or config.week_start()
    week = week_start.isoformat()

    outstanding = [
        row for row in _open_followups(week)
        if not row.get("recipient_id") or row["recipient_id"] == reply.sender_id
    ]
    if not outstanding:
        print("      no open question for this sender - ignoring")
        return None
    row = max(outstanding, key=lambda r: r["created_at"])

    option = instagram.match_option(reply, row["options"] or [])
    if not option:
        instagram.send_dm(
            reply.sender_id,
            "Sorry, I didn't catch that. Reply with just the number of the "
            "option you want:\n"
            + "\n".join(f"{o['key']}. {o['label']}" for o in row["options"] or []),
        )
        log_eval(STAGE, f"{week}:reply", False,
                 error_message=f"unmatched reply: {reply.text[:80]}")
        return None

    print(f"      user chose {option['key']}: {option['label']}")
    resolution = apply_option(week_start, option, use_llm=use_llm)
    db.update("followups", row["id"], {
        "status": "answered",
        "reply_text": reply.text,
        "chosen_key": option["key"],
        "resolution": resolution,
        "answered_at": config.now_local().isoformat(),
    })
    log_eval(STAGE, f"{week}:reply", True)

    # The calendar has to match the plan the user just changed.
    calendar_sync.run(week_start=week_start)

    # Recurse: the new plan gets the same feasibility check as the old one.
    remaining = run(week_start=week_start, round_number=row["round"] + 1,
                    use_llm=use_llm, announce=False)
    if not remaining:
        instagram.send_dm(
            reply.sender_id,
            f"Done - {resolution}. Your calendar is updated and the week works.",
        )
    return dict(row, status="answered", resolution=resolution)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def auto_resolve(week_start: date, issue: dict, *, use_llm: bool = True) -> str:
    """What to do when nobody can answer: the old deterministic behaviour.

    Kept as the floor, not the first move. A delivery conflict loses the slot; an
    unresolvable ingredient is left for the cook, who is told in the invite.
    """
    if issue["kind"] == ISSUE_DELIVERY and issue.get("slot_id"):
        _rebuild(week_start, use_llm=use_llm, exclude_slots=(issue["slot_id"],))
        return "no reply reachable - re-planned without that window"
    return "no reply reachable - left the missing items for the cook to pick up"


def run(
    week_start: date | None = None,
    *,
    round_number: int = 1,
    dry_run: bool = False,
    use_llm: bool = True,
    auto: bool = True,
    announce: bool = True,
) -> dict | None:
    """Check the finished week, and ask about the first thing that needs a human.

    Returns the issue it acted on, or None when the week is already feasible.
    """
    week_start = week_start or config.week_start()
    week = week_start.isoformat()
    issues = open_issues(week_start)

    if announce:
        print(f"[5b/6] followup     week of {week_start} | "
              f"{len(issues)} issue(s) needing a decision")
    if not issues:
        if announce:
            print("      the week is feasible - nothing to ask")
        return None

    issue = issues[0]
    print(f"      [!] {issue['detail']}")

    if round_number > config.FOLLOWUP_MAX_ROUNDS:
        # The bound exists so this cannot ping-pong. Say so out loud rather
        # than quietly stopping.
        resolution = auto_resolve(week_start, issue, use_llm=use_llm)
        print(f"      round limit reached ({config.FOLLOWUP_MAX_ROUNDS}) - "
              f"{resolution}")
        log_eval(STAGE, f"{week}:round-limit", False,
                 error_message="follow-up round limit reached")
        return issue

    already_open = [r for r in _open_followups(week) if r["kind"] == issue["kind"]]
    if already_open and not dry_run:
        print(f"      already asked about this (round "
              f"{already_open[-1]['round']}) - waiting for a reply")
        return issue

    row = ask(week_start, issue, round_number=round_number, dry_run=dry_run)
    if row and row["status"] == "unreachable" and auto:
        resolution = auto_resolve(week_start, issue, use_llm=use_llm)
        db.update("followups", row["id"], {"resolution": resolution})
        print(f"      {resolution}")
    return issue


def status(week_start: date | None = None) -> list[dict]:
    week_start = week_start or config.week_start()
    rows = db.select("followups", "*", week_start_date=week_start.isoformat())
    rows.sort(key=lambda r: r["created_at"])
    print(f"[5b/6] followup     week of {week_start} | {len(rows)} question(s)")
    for row in rows:
        print(f"      round {row['round']} {row['kind']} via {row['channel']} "
              f"-> {row['status']}")
        if row.get("chosen_key"):
            print(f"        chose {row['chosen_key']}: {row.get('resolution')}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and print the DM; write nothing")
    parser.add_argument("--reply", metavar="TEXT",
                        help="act as if the user replied this")
    parser.add_argument("--status", action="store_true",
                        help="show this week's conversation")
    parser.add_argument("--no-reasons", action="store_true",
                        help="skip the score_reason model call when re-planning")
    args = parser.parse_args()
    week_start = date.fromisoformat(args.week) if args.week else None

    if args.status:
        status(week_start)
        return
    if args.reply:
        sender = db.latest_sender_id() or "local"
        apply_reply(
            instagram.TextReply(sender_id=sender, text=args.reply,
                                payload=None, message_id=None),
            week_start=week_start,
            use_llm=not args.no_reasons,
        )
        return
    run(week_start=week_start, dry_run=args.dry_run, use_llm=not args.no_reasons)


if __name__ == "__main__":
    main()
