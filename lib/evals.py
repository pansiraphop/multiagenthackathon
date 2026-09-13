"""eval_log writes and reporting.

Reliability & evaluation is 25% of the hackathon score — second only to
technical execution. This table is a graded deliverable, not instrumentation.
Every stage writes to it on success AND failure, no exceptions.

Because of that, log_eval must never raise. If logging itself fails we print a
warning and carry on: losing a log line is bad, but taking down a pipeline
stage because the logger broke is worse.
"""

from __future__ import annotations

import statistics
import time
from contextlib import contextmanager

from lib import db

STAGES = (
    "ingestion", "extraction", "availability", "planning",
    "shopping_list", "instacart", "calendar",
)


def log_eval(
    stage: str,
    input_ref: str | None = None,
    success: bool = True,
    retry_count: int = 0,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> None:
    """Record one stage attempt. Swallows its own failures by design."""
    try:
        db.insert("eval_log", {
            "stage": stage,
            "input_ref": str(input_ref) if input_ref is not None else None,
            "success": success,
            "retry_count": retry_count,
            "duration_ms": duration_ms,
            # Postgres text has no hard limit, but keep rows readable in reports.
            "error_message": error_message[:500] if error_message else None,
        })
    except Exception as exc:                                   # noqa: BLE001
        print(f"  [warn] eval_log write failed ({stage}): {exc}")


@contextmanager
def timed(stage: str, input_ref: str | None = None):
    """Time a block and log it either way.

    with timed("availability", str(week)):
        ...

    Re-raises after logging the failure, so callers still control flow.
    """
    t0 = time.time()
    try:
        yield
    except Exception as exc:                                   # noqa: BLE001
        log_eval(stage, input_ref, False,
                 duration_ms=int((time.time() - t0) * 1000),
                 error_message=str(exc))
        raise
    else:
        log_eval(stage, input_ref, True,
                 duration_ms=int((time.time() - t0) * 1000))


def eval_report() -> dict[str, dict]:
    """Per-stage attempts, success rate, avg retries, p50/p95 latency, errors.

    This is what run_eval.py prints and what the reliability brief quotes.
    """
    rows = db.select("eval_log")
    report: dict[str, dict] = {}

    for stage in sorted({r["stage"] for r in rows}):
        stage_rows = [r for r in rows if r["stage"] == stage]
        durations = sorted(r["duration_ms"] for r in stage_rows if r["duration_ms"])
        successes = [r for r in stage_rows if r["success"]]
        report[stage] = {
            "attempts": len(stage_rows),
            "succeeded": len(successes),
            "success_rate": len(successes) / len(stage_rows) if stage_rows else 0.0,
            "avg_retries": (
                statistics.mean(r["retry_count"] or 0 for r in stage_rows)
                if stage_rows else 0.0
            ),
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "errors": [r["error_message"] for r in stage_rows if r["error_message"]],
        }
    return report


def _percentile(sorted_values: list[int], pct: int) -> int | None:
    if not sorted_values:
        return None
    # Nearest-rank. With a handful of samples this is honest; interpolating
    # across 6 data points would imply precision we don't have.
    k = max(0, min(len(sorted_values) - 1,
                   int(round((pct / 100) * len(sorted_values) + 0.5)) - 1))
    return sorted_values[k]


def format_report(report: dict[str, dict] | None = None) -> str:
    """Markdown table that pastes straight into the reliability brief."""
    report = report if report is not None else eval_report()
    if not report:
        return "_No eval_log rows yet._"

    lines = [
        "| Stage | Attempts | Success | Avg retries | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|",
    ]
    for stage, m in report.items():
        lines.append(
            f"| {stage} | {m['attempts']} | {m['succeeded']}/{m['attempts']} "
            f"({m['success_rate']:.0%}) | {m['avg_retries']:.2f} | "
            f"{m['p50_ms'] or '—'} | {m['p95_ms'] or '—'} |"
        )

    errors = [(s, e) for s, m in report.items() for e in m["errors"]]
    if errors:
        lines.append("")
        lines.append("**Failures**")
        for stage, err in errors:
            lines.append(f"- `{stage}`: {err}")
    return "\n".join(lines)
