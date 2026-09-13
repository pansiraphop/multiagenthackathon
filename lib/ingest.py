"""Instagram Reel ingestion: webhook payload -> pending recipe row.

The webhook caption is the primary source. Audio transcription is deliberately
best-effort: a download, ffmpeg, or local Whisper failure must not discard a
useful caption.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import config
from lib import db
from lib.evals import log_eval
from lib.external import call_external_api


@dataclass(frozen=True)
class ReelIngest:
    sender_id: str | None
    reel_video_id: str | None
    source_url: str
    caption: str | None


def parse_reel_attachments(payload: dict[str, Any]) -> list[ReelIngest]:
    """Extract every ``ig_reel`` attachment from an Instagram webhook payload."""
    if payload.get("object") != "instagram":
        return []

    reels: list[ReelIngest] = []
    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            sender_id = (event.get("sender") or {}).get("id")
            message = event.get("message") or {}
            for attachment in message.get("attachments") or []:
                if attachment.get("type") != "ig_reel":
                    continue
                reel_payload = attachment.get("payload") or {}
                source_url = reel_payload.get("url")
                if not source_url:
                    continue
                reels.append(
                    ReelIngest(
                        sender_id=str(sender_id) if sender_id else None,
                        reel_video_id=_optional_string(
                            reel_payload.get("reel_video_id")
                        ),
                        source_url=str(source_url),
                        caption=fetch_caption(reel_payload),
                    )
                )
    return reels


def fetch_caption(reel_payload: dict[str, Any]) -> str | None:
    """Return Meta's caption/title, which is the preferred recipe source."""
    title = reel_payload.get("title")
    if not isinstance(title, str):
        return None
    caption = title.strip()
    return caption or None


def fetch_transcript(reel_url: str) -> str | None:
    """Download Reel audio and transcribe it locally, returning None on failure."""
    transcript, ok = call_external_api(
        _download_and_transcribe,
        reel_url,
        stage="ingestion",
        input_ref=f"transcript:{reel_url}",
    )
    if not ok or not isinstance(transcript, str):
        return None
    transcript = transcript.strip()
    return transcript or None


def persist_pending_recipe(
    source_url: str,
    caption: str | None,
    transcript: str | None,
    sender_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert one pending recipe, or return the existing row for this URL."""
    existing = db.select("recipes", "*", source_url=source_url)
    if existing:
        return existing[0], False

    row = db.insert_recipe(
        source_url=source_url,
        # Kept so stage 5b can DM the person who sent the reel, and so the
        # swap options it offers are their own earlier reels.
        sender_id=sender_id,
        raw_caption=caption,
        raw_transcript=transcript,
        extraction_status="pending",
    )
    return row, True


def ingest_reel(reel: ReelIngest) -> dict[str, Any] | None:
    """Persist one attachment for extraction; designed for a background thread."""
    started = time.monotonic()
    try:
        existing = db.select("recipes", "*", source_url=reel.source_url)
        if existing:
            return existing[0]

        transcript = (
            fetch_transcript(reel.source_url) if config.INGEST_TRANSCRIBE else None
        )
        if not reel.caption and not transcript:
            raise ValueError("Reel has neither a caption nor a usable transcript")

        row, inserted = persist_pending_recipe(
            reel.source_url,
            reel.caption,
            transcript,
            reel.sender_id,
        )
        if inserted:
            log_eval(
                "ingestion",
                input_ref=reel.source_url,
                success=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return row
    except Exception as exc:  # noqa: BLE001 - background jobs must not kill the app
        log_eval(
            "ingestion",
            input_ref=reel.source_url,
            success=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_message=str(exc),
        )
        print(f"  [warn] Reel ingestion failed ({reel.source_url}): {exc}")
        return None


def _download_and_transcribe(reel_url: str) -> str:
    """Blocking yt-dlp + Whisper implementation, isolated for error wrapping."""
    import whisper
    from yt_dlp import YoutubeDL

    with TemporaryDirectory(prefix="instacook-") as temp_dir:
        output_template = str(Path(temp_dir) / "reel.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        with YoutubeDL(options) as downloader:
            downloader.download([reel_url])

        audio_path = Path(temp_dir) / "reel.mp3"
        if not audio_path.exists():
            raise RuntimeError("yt-dlp completed without producing reel.mp3")

        result = _whisper_model().transcribe(str(audio_path), fp16=False)
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Whisper returned an empty transcript")
        return text.strip()


@functools.lru_cache(maxsize=1)
def _whisper_model():
    import whisper

    return whisper.load_model(config.WHISPER_MODEL)


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
