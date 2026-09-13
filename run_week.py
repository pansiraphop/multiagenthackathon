"""InstaCook — run the whole week in one command.

    python run_week.py --dry-run        # read-only: no calendar writes, no cart
    python run_week.py                  # plan + shop + schedule (NO ordering)
    python run_week.py --instacart      # ...and build the Instacart cart too
    python run_week.py --from plan      # resume from a given stage

Stage 5 is OPT-IN. Building a cart is the one step with real-world consequences
and it is still being built, so nothing here touches Instacart unless you ask
for it by name. Everything else is safe to re-run: every stage is idempotent
and the calendar can be rolled back with `--clear`.

Stage 5b (`stages/followup.py`) runs after the cart and closes the loop: if the
week is no longer feasible it DMs the user with options instead of quietly
re-planning. The answer arrives through `webhook.py`, not through this script,
so a run can legitimately end with a question outstanding.

The narration is the point. Each stage prints what it decided, so the run reads
as reasoning rather than as a log.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import config
from lib import db
from lib.evals import format_report
from stages import availability, calendar_sync, plan, shopping_list

STAGES = ("extract", "availability", "plan", "shopping", "instacart",
          "followup", "calendar")


def _rule(text: str = "") -> None:
    print(f"\n{text}" if text else "")


def run_extract() -> int:
    """Stage 1 over whatever ingestion has left pending."""
    pending = db.pending_recipes()
    if not pending:
        print(f"[1/6] extract       nothing pending "
              f"({len(db.successful_recipes())} recipes already extracted)")
        return 0

    from stages import extract
    print(f"[1/6] extract       {len(pending)} pending reel(s)")
    done = 0
    for row in pending:
        if extract.extract_recipe(row):
            done += 1
    print(f"      extracted {done}/{len(pending)}")
    return done


def handle_feedback(week_start: date, use_llm: bool, dry_run: bool = False) -> bool:
    """The feedback edge: stage 5's answer can invalidate stage 3's plan.

    If the groceries can't land before the earliest cook slot, or the cart
    couldn't resolve what a meal needs, the week isn't feasible any more. Stage
    5b asks the user over Instagram DM — reschedule, or swap in one of their own
    earlier reels — and only resolves it alone when nobody can be reached.

    This is the one place the pipeline reconsiders its own output because the
    physical world said no, and it's the clearest answer to "is this an agent or
    a cron job?"
    """
    from stages import followup

    print()
    issue = followup.run(week_start=week_start, use_llm=use_llm, dry_run=dry_run)
    print()
    return issue is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--from", dest="start_at", choices=STAGES,
                        default="extract", help="resume from this stage")
    parser.add_argument("--dry-run", action="store_true",
                        help="read-only: no calendar writes and no cart")
    parser.add_argument("--instacart", action="store_true",
                        help="also build the Instacart cart (stage 5, opt-in)")
    parser.add_argument("--no-reasons", action="store_true",
                        help="skip the score_reason model call")
    parser.add_argument("--offline", action="store_true",
                        help="skip Google free/busy, use fallback slots")
    parser.add_argument("--voiceover", action="store_true",
                        help="also narrate the week with ElevenLabs (stage 7)")
    args = parser.parse_args()

    week_start = date.fromisoformat(args.week) if args.week else config.week_start()
    begin = STAGES.index(args.start_at)
    use_llm = not args.no_reasons

    mode = []
    if args.dry_run:
        mode.append("dry run")
    if not args.instacart:
        mode.append("no ordering")
    print(f"InstaCook | week of {week_start}"
          + (f" | {', '.join(mode)}" if mode else ""))
    _rule()

    if begin <= STAGES.index("extract"):
        run_extract()

    if begin <= STAGES.index("availability"):
        if not availability.run(week_start=week_start, offline=args.offline):
            print("      no cook windows - stopping")
            return 1

    if begin <= STAGES.index("plan"):
        if not plan.run(week_start=week_start, use_llm=use_llm):
            print("      nothing could be planned - stopping")
            return 1

    if begin <= STAGES.index("shopping"):
        shopping_list.run(week_start=week_start)

    if begin <= STAGES.index("instacart"):
        if args.instacart and not args.dry_run:
            from stages import instacart
            instacart.run(week_start=week_start)
        else:
            print("[5/6] instacart     skipped (pass --instacart to build a cart)")

    # Reads rows only, so it still checks whatever cart an earlier run left
    # behind — and on a dry run it decides out loud without sending anything.
    if begin <= STAGES.index("followup"):
        handle_feedback(week_start, use_llm, dry_run=args.dry_run)

    if begin <= STAGES.index("calendar"):
        calendar_sync.run(week_start=week_start, dry_run=args.dry_run)

    # Stage 7 is a separate workflow, not a link in the chain: it reads the
    # rows the run just wrote and nothing depends on what it produces, so it
    # is opt-in and can never take the pipeline down with it.
    if args.voiceover:
        from stages import voiceover
        voiceover.run_week(week_start=week_start, script_only=args.dry_run)

    _rule()
    print(format_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
