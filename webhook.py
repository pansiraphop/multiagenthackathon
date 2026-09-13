"""Instagram webhook receiver: Reels in, answers back.

Meta expects webhook requests to return quickly, so both handlers run as FastAPI
background tasks after the payload has been acknowledged.

Two kinds of message arrive on this one endpoint. A reel attachment is a new
recipe (stage 1). A plain text message may be the answer to a question stage 5b
asked when the week stopped being feasible — that is the return leg of the
feedback loop, and it is what makes the pipeline conversational rather than
one-shot.
"""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

import config
from lib.ingest import ingest_reel, parse_reel_attachments
from lib.instagram import TextReply, parse_text_replies

app = FastAPI(title="InstaCook Instagram webhook")


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> str:
    """Complete Meta's webhook verification challenge."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and config.META_VERIFY_TOKEN
        and params.get("hub.verify_token") == config.META_VERIFY_TOKEN
    ):
        challenge = params.get("hub.challenge")
        if challenge is not None:
            return challenge
    raise HTTPException(status_code=403, detail="verification failed")


def _has_open_question(sender_id: str) -> bool:
    """Is stage 5b waiting on an answer from this person?

    OPEN_STATUS is imported rather than written out: it is "sent", not "open",
    and hardcoding the wrong value here fails silently — every answer would be
    routed to the concierge and the follow-up loop would never close.
    """
    try:
        from lib import db
        from stages.followup import OPEN_STATUS

        return any(
            row.get("recipient_id") in (None, sender_id)
            for row in db.select("followups", "*", status=OPEN_STATUS)
        )
    except Exception:  # noqa: BLE001 - never let this decide by crashing
        return False


def handle_reply(reply: TextReply) -> None:
    """Route one inbound text message.

    Two different things can arrive as text. If stage 5b has an open question,
    this is the answer to it and goes back into that state machine. Otherwise
    it's conversation, and the concierge agent handles it — that agent's tools
    are the pipeline stages, so anything the CLI can do can be asked for here.

    Both are imported inside the function rather than at module scope so that a
    missing Google or Instacart dependency can never stop the webhook accepting
    reels — ingestion is the one thing this process must always do.
    """
    try:
        if _has_open_question(reply.sender_id):
            from stages import followup

            followup.apply_reply(reply)
            return
    except Exception as exc:  # noqa: BLE001 - background tasks must not kill the app
        print(f"  [warn] follow-up reply failed ({reply.sender_id}): {exc}")

    try:
        from stages import concierge

        concierge.handle_dm(reply.sender_id, reply.text)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] concierge failed ({reply.sender_id}): {exc}")


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, int | str]:
    """Acknowledge a Meta delivery, then handle reels and replies off-path."""
    payload = await request.json()
    reels = parse_reel_attachments(payload)
    for reel in reels:
        background_tasks.add_task(ingest_reel, reel)

    replies = parse_text_replies(payload)
    for reply in replies:
        background_tasks.add_task(handle_reply, reply)

    return {
        "status": "received",
        "reels_queued": len(reels),
        "replies_queued": len(replies),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
