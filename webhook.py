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


def handle_reply(reply: TextReply) -> None:
    """Apply one inbound answer to the open follow-up question.

    stages.followup is imported here rather than at module scope so that a
    missing Google or Instacart dependency can never stop the webhook from
    accepting reels — ingestion is the one thing this process must always do.
    """
    try:
        from stages import followup

        followup.apply_reply(reply)
    except Exception as exc:  # noqa: BLE001 - background tasks must not kill the app
        print(f"  [warn] follow-up reply failed ({reply.sender_id}): {exc}")


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
