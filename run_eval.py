"""Reliability report — what actually happened, across every stage.

    python run_eval.py              # the report
    python run_eval.py --failures   # ...and every recorded failure in full

Every stage writes to `eval_log` on success and on failure, with its retry
count and how long it took. This reads that table back. The numbers below are
from real runs against real services — the Anthropic API, Google Calendar,
Instacart via Browserbase, Instagram's Graph API and Supabase — not from a
simulation.

Two things worth knowing about how to read it:

Success rate is per attempt, not per outcome. A stage that failed once and
succeeded on retry shows as two attempts, one failure. That's deliberate: the
retry is the interesting part, and hiding it would make the system look more
reliable than it is.

A high p95 is usually a fallback working, not a stall. Extraction's tail is the
reconstruction path — identify the dish, search the web, re-extract — which
only runs when a reel has no usable recipe in it.
"""

from __future__ import annotations

import argparse

from lib.evals import eval_report, format_report


def headline(report: dict[str, dict]) -> str:
    attempts = sum(m["attempts"] for m in report.values())
    succeeded = sum(m["succeeded"] for m in report.values())
    failures = attempts - succeeded
    rate = (succeeded / attempts) if attempts else 0.0
    return (f"{attempts} logged attempts across {len(report)} stages, "
            f"{succeeded} succeeded ({rate:.1%}), {failures} failed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failures", action="store_true",
                        help="print every recorded failure, not just the counts")
    args = parser.parse_args()

    report = eval_report()
    if not report:
        print("No eval_log rows yet. Run the pipeline first: python run_week.py")
        return 1

    print("InstaCook — reliability report")
    print("=" * 66)
    print(headline(report))
    print()
    print(format_report(report))

    if args.failures:
        print()
        print("Recorded failures")
        print("-" * 66)
        for stage, metrics in report.items():
            for error in metrics["errors"]:
                print(f"  [{stage}] {error}")

    print()
    print("Every row above is a real call to a real service. Failures are left")
    print("in rather than filtered out — a stage that never fails is a stage")
    print("that was never exercised.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
