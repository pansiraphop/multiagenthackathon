"""The two LLM wrappers. Write these once — don't end up with two retry patterns.

`client.messages.parse()` with a Pydantic model guarantees schema-valid JSON at
the API level, so the retry loop here is for SEMANTIC validation (unit outside
the enum, negative quantity, empty steps) and transient API errors — not for
JSON parsing. That also means "100% schema valid" is a vanity metric; measure
accuracy against ground truth instead.
"""

from __future__ import annotations

import functools
import time
from typing import Callable, TypeVar

import anthropic
from pydantic import BaseModel

import config
from lib.evals import log_eval

T = TypeVar("T", bound=BaseModel)

MAX_TOKENS = 16000        # non-streaming: keeps us under the SDK HTTP timeout


class LLMFailure(RuntimeError):
    """Raised when a structured call can't produce a valid result in max_retries."""


@functools.lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY missing — fill it in .env")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def call_llm_structured(
    prompt: str,
    output_model: type[T],
    *,
    stage: str,
    input_ref: str | None = None,
    validate: Callable[[T], list[str]] | None = None,
    system: str | None = None,
    max_retries: int = 3,
) -> T:
    """Return a validated `output_model` instance, or raise LLMFailure.

    `validate` returns a list of human-readable problems; an empty list means
    the result is good. Problems are fed back to the model verbatim, which is
    far more effective than a generic "try again".
    """
    t0 = time.time()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_error: str | None = None

    for attempt in range(max_retries):
        try:
            response = client().messages.parse(
                model=config.MODEL,
                max_tokens=MAX_TOKENS,
                system=system or anthropic.NOT_GIVEN,
                messages=messages,
                output_format=output_model,
            )
            parsed = response.parsed_output
            problems = validate(parsed) if validate else []
            if not problems:
                log_eval(stage, input_ref, True, attempt,
                         int((time.time() - t0) * 1000))
                return parsed

            last_error = "; ".join(problems)
            # Echo the bad result back and name every problem. This is a normal
            # assistant turn followed by a user turn, NOT a prefill (prefills
            # 400 on claude-opus-5).
            messages += [
                {"role": "assistant", "content": parsed.model_dump_json()},
                {"role": "user", "content":
                    "Those values failed validation:\n- " + "\n- ".join(problems)
                    + "\nReturn the corrected object."},
            ]

        except anthropic.NotFoundError as exc:
            last_error = f"model not found: {exc}"
            break                                   # not retryable
        except anthropic.RateLimitError as exc:
            last_error = f"rate limited: {exc}"
            time.sleep(2 ** attempt)
        except anthropic.APIStatusError as exc:
            last_error = f"HTTP {exc.status_code}: {exc}"
            if exc.status_code == 400:
                break                               # our request is wrong; retrying won't help
            time.sleep(2 ** attempt)
        except anthropic.APIConnectionError as exc:
            last_error = f"connection error: {exc}"
            time.sleep(2 ** attempt)

    log_eval(stage, input_ref, False, max_retries,
             int((time.time() - t0) * 1000), error_message=last_error)
    raise LLMFailure(f"{stage} failed for {input_ref}: {last_error}")


def call_llm_with_search(
    prompt: str,
    *,
    stage: str,
    input_ref: str | None = None,
    max_uses: int = 3,
    system: str | None = None,
) -> str | None:
    """Free-form call with Anthropic's server-side web search. Returns text or None.

    Used by the reconstruction path in stage 1: research the dish, emit prose,
    then run that prose back through the normal extractor. Deliberately NOT
    structured — the extractor already owns the schema.

    Do not also declare code_execution: this web_search variant runs it
    internally and a second execution environment confuses the model.
    """
    t0 = time.time()
    try:
        response = client().messages.create(
            model=config.MODEL,
            max_tokens=MAX_TOKENS,
            system=system or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": max_uses,
            }],
        )
    except Exception as exc:                                   # noqa: BLE001
        log_eval(stage, input_ref, False,
                 duration_ms=int((time.time() - t0) * 1000),
                 error_message=str(exc))
        return None

    # Server-tool errors arrive as HTTP 200 with an error OBJECT where a result
    # LIST is expected. Branch on that or you get a confusing TypeError.
    search_failed = False
    for block in response.content:
        if getattr(block, "type", None) == "web_search_tool_result":
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                search_failed = True

    text = "\n".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()

    if not text:
        log_eval(stage, input_ref, False,
                 duration_ms=int((time.time() - t0) * 1000),
                 error_message="search call returned no text")
        return None

    # Search failing but the model still answering from its own knowledge is a
    # degraded success, not a failure — log it so the brief can say so.
    log_eval(stage, input_ref, True,
             duration_ms=int((time.time() - t0) * 1000),
             error_message="web_search unavailable; answered unsourced"
             if search_failed else None)
    return text
