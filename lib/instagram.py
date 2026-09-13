"""Outbound Instagram DMs and inbound reply parsing (stage 5b).

Two tiers, same as everywhere else in this codebase. With
`INSTAGRAM_ACCESS_TOKEN` set, questions go out through the Instagram Send API
and the user answers in the same thread they sent the reels from. Without one,
the question is printed to the console and reported as **undelivered** — which
is the honest answer, because nobody can reply to a print statement, and stage
5b uses that signal to resolve the week on its own instead of waiting.

Replies are matched on three things, in order: the quick-reply payload, a bare
number, then a keyword. People answer "2", "two", "reschedule" and "the noodles
please" — only the first is guaranteed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests

import config
from lib.external import call_external_api

STAGE = "instagram_dm"
# Meta's limit for a quick-reply title. Longer titles are rejected outright, so
# they are truncated rather than allowed to fail the send.
QUICK_REPLY_TITLE_MAX = 20
PAYLOAD_PREFIX = "INSTACOOK_"


@dataclass(frozen=True)
class TextReply:
    """An inbound text message: a candidate answer to an open question."""
    sender_id: str
    text: str
    payload: str | None
    message_id: str | None


@dataclass(frozen=True)
class Delivery:
    """The outcome of one send attempt."""
    delivered: bool
    channel: str            # 'instagram_dm' | 'console'
    message_id: str | None
    error: str | None = None


def configured() -> bool:
    return bool(config.INSTAGRAM_ACCESS_TOKEN)


def _endpoint() -> str:
    base = config.INSTAGRAM_API_BASE.rstrip("/")
    return f"{base}/{config.INSTAGRAM_API_VERSION}/me/messages"


def send_dm(
    recipient_id: str | None,
    text: str,
    options: list[dict] | None = None,
) -> Delivery:
    """Send one DM. Never raises — a failed question must not stop the week."""
    if not recipient_id or not configured():
        reason = (
            "no INSTAGRAM_ACCESS_TOKEN" if not configured() else "no recipient id"
        )
        print(f"\n      [dm not sent: {reason} - printing instead]")
        for line in text.splitlines():
            print(f"      | {line}")
        print()
        return Delivery(False, "console", None, error=reason)

    message: dict[str, Any] = {"text": text}
    if options:
        message["quick_replies"] = [
            {
                "content_type": "text",
                "title": str(option["label"])[:QUICK_REPLY_TITLE_MAX],
                "payload": f"{PAYLOAD_PREFIX}{option['key']}",
            }
            for option in options
        ]

    result, ok = call_external_api(
        _post,
        {"recipient": {"id": str(recipient_id)}, "message": message},
        stage=STAGE,
        input_ref=str(recipient_id),
    )
    if not ok or not isinstance(result, dict):
        return Delivery(False, "instagram_dm", None, error="send failed")
    return Delivery(True, "instagram_dm", result.get("message_id"))


def _post(body: dict) -> dict:
    response = requests.post(
        _endpoint(),
        json=body,
        headers={"Authorization": f"Bearer {config.INSTAGRAM_ACCESS_TOKEN}"},
        timeout=config.DM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def parse_text_replies(payload: dict[str, Any]) -> list[TextReply]:
    """Pull answerable text messages out of a webhook payload.

    Echoes of our own outgoing messages and reel attachments are both skipped:
    the first would have the agent answering itself, and the second is stage 1's
    job (see lib/ingest.parse_reel_attachments).
    """
    if payload.get("object") != "instagram":
        return []

    replies: list[TextReply] = []
    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            message = event.get("message") or {}
            if message.get("is_echo"):
                continue
            if message.get("attachments"):
                continue
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                continue
            quick_reply = message.get("quick_reply") or {}
            replies.append(
                TextReply(
                    sender_id=str(sender_id),
                    text=text.strip(),
                    payload=quick_reply.get("payload"),
                    message_id=message.get("mid"),
                )
            )
    return replies


NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "first": "1", "second": "2", "third": "3",
}
# Deliberately narrow. A word here has to mean the action unambiguously,
# otherwise a wrong guess silently reschedules someone's real evening.
ACTION_WORDS = {
    "later": ("later", "reschedule", "move", "postpone", "push"),
    "swap": ("swap", "instead", "different", "other", "replace"),
    "keep": ("keep", "as planned", "leave it", "no change", "fine", "go ahead"),
}


def match_option(reply: TextReply, options: list[dict]) -> dict | None:
    """Resolve a reply to one offered option, or None when it's ambiguous."""
    if not options:
        return None
    by_key = {str(o["key"]): o for o in options}

    if reply.payload and reply.payload.startswith(PAYLOAD_PREFIX):
        chosen = by_key.get(reply.payload[len(PAYLOAD_PREFIX):])
        if chosen:
            return chosen

    text = reply.text.strip().lower()
    # "2", "#3 please" and "yes, 1" are all one answer. "1 or 2" is not an
    # answer at all, so a reply naming two options is left unmatched.
    numbers = set(re.findall(r"\b\d+\b", text))
    if len(numbers) == 1:
        only = numbers.pop()
        if only in by_key:
            return by_key[only]

    first_word = re.split(r"[^a-z]+", text, maxsplit=1)[0]
    if first_word in NUMBER_WORDS and NUMBER_WORDS[first_word] in by_key:
        return by_key[NUMBER_WORDS[first_word]]

    # An exact label match is unambiguous; a partial one is not, so a reply
    # matching two options' labels is treated as no answer at all.
    labelled = [o for o in options if str(o["label"]).lower() == text]
    if len(labelled) == 1:
        return labelled[0]

    matched_actions = {
        action for action, words in ACTION_WORDS.items()
        if any(word in text for word in words)
    }
    if len(matched_actions) == 1:
        action = matched_actions.pop()
        candidates = [o for o in options if o.get("action") == action]
        if len(candidates) == 1:
            return candidates[0]
    return None
