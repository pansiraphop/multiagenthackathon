"""The external-API wrapper. Used for every Instacart call and every Calendar
read/write.

Log and swallow: one failed item must never kill the batch. The caller decides
the fallback — that is what makes the Instacart tier ladder and the availability
default-slots fallback possible.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from lib.evals import log_eval


def call_external_api(
    fn: Callable[..., Any],
    *args,
    stage: str,
    input_ref: str | None = None,
    **kwargs,
) -> tuple[Any, bool]:
    """Return (result, ok). Never raises.

    ok is False when the call failed, so callers can branch without inspecting
    the result for a sentinel:

        slots, ok = call_external_api(freebusy_query, week,
                                      stage="availability", input_ref=str(week))
        if not ok:
            slots = default_evening_slots(week)
    """
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:                                   # noqa: BLE001
        log_eval(stage, input_ref, False,
                 duration_ms=int((time.time() - t0) * 1000),
                 error_message=str(exc))
        return None, False
    else:
        log_eval(stage, input_ref, True,
                 duration_ms=int((time.time() - t0) * 1000))
        return result, True
