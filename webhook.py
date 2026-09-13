"""Instagram webhook receiver for Reel ingestion.

Meta expects webhook requests to return quickly, so caption/transcript ingestion
runs as a FastAPI background task after the payload has been acknowledged.
"""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

import config
from lib.ingest import ingest_reel, parse_reel_attachments

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


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, int | str]:
    """Acknowledge a Meta delivery and ingest each Reel attachment off-path."""
    payload = await request.json()
    reels = parse_reel_attachments(payload)
    for reel in reels:
        background_tasks.add_task(ingest_reel, reel)
    return {"status": "received", "reels_queued": len(reels)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
