"""ElevenLabs text-to-speech. The only place in the repo that talks to it.

Same contract as every other external call in this project (ground rule 7):
log and return None, never raise at the caller. Stage 7 then decides the
fallback, which is always "you still have the script as text" — a voiceover
that can't be spoken is a smaller loss than a workflow that dies.

Deliberately plain `requests` rather than the ElevenLabs SDK: two endpoints,
no streaming, and one less dependency to install at 3 PM.
"""

from __future__ import annotations

import re

import requests

import config
from lib.evals import log_eval
from lib.external import call_external_api

STAGE = "voiceover"

# Voices that work on a free account without the voices_read permission, so
# `--voice George` resolves even when a restricted key can't list anything.
# Rachel and Aria are deliberately absent: they are library voices and a free
# key gets HTTP 402 for them.
DEFAULT_VOICES = {
    "sarah": "EXAVITQu4vr4xnSDxMaL",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "brian": "nPczCjzI2devNBz1zQrb",
}

# The last message ElevenLabs sent back. call_external_api logs failures to
# eval_log and returns None by design, which is right for control flow and
# useless to someone watching the terminal — this is how the reason reaches
# them without unwinding the wrapper.
_last_error: str | None = None


def _headers(accept: str) -> dict[str, str]:
    return {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "accept": accept,
        "content-type": "application/json",
    }


def configured() -> bool:
    return bool(config.ELEVENLABS_API_KEY)


def _raise_for_elevenlabs(response: requests.Response) -> None:
    """Surface ElevenLabs' own error text, which is far more useful than the status.

    An invalid key, an unknown voice and an exhausted quota are all 401/422 with
    a JSON body naming the actual problem — losing that body costs ten minutes
    of guessing.
    """
    global _last_error
    if response.ok:
        return
    try:
        body = response.json()
        detail = str(body.get("detail") or body)
    except ValueError:
        detail = response.text[:200]
    _last_error = f"HTTP {response.status_code}: {detail}"
    raise RuntimeError(f"ElevenLabs {_last_error}")


# --- voices ----------------------------------------------------------------

def _fetch_voices() -> list[dict]:
    response = requests.get(
        f"{config.ELEVENLABS_API_BASE}/v2/voices",
        headers=_headers("application/json"),
        params={"page_size": 100},
        timeout=config.ELEVENLABS_TIMEOUT_SECONDS,
    )
    _raise_for_elevenlabs(response)
    return response.json().get("voices", [])


def available_voices() -> list[dict]:
    """Voices on the account. Empty list when the call fails — never raises."""
    if not configured():
        return []
    voices, ok = call_external_api(_fetch_voices, stage=STAGE, input_ref="voices")
    return voices if ok and voices else []


def resolve_voice(name_or_id: str | None = None) -> str:
    """Accept either a voice id or a voice name, and return an id.

    Names are what a human remembers between runs; ids are what the API wants.
    An unknown name falls through to the configured default rather than
    failing, because a demo narrated in the wrong voice still narrates.
    """
    wanted = (name_or_id or config.ELEVENLABS_VOICE).strip()
    if not wanted:
        return config.ELEVENLABS_VOICE
    # ElevenLabs ids are 20-character opaque strings; a name is short or spaced.
    if len(wanted) >= 20 and " " not in wanted:
        return wanted
    if wanted.lower() in DEFAULT_VOICES:
        return DEFAULT_VOICES[wanted.lower()]

    for voice in available_voices():
        if voice.get("name", "").lower() == wanted.lower():
            return voice["voice_id"]
        if voice.get("voice_id") == wanted:
            return wanted

    print(f"      [warn] no voice named '{wanted}' on this account - "
          f"using {config.ELEVENLABS_VOICE}")
    return config.ELEVENLABS_VOICE


# --- synthesis -------------------------------------------------------------

def split_for_tts(text: str, max_chars: int | None = None) -> list[str]:
    """Chunk long narration on boundaries a listener can't hear.

    Paragraph breaks first, sentence breaks only when a single paragraph is
    itself too long. MP3 frames concatenate cleanly, so the stitched file plays
    as one take.
    """
    max_chars = max_chars or config.TTS_MAX_CHARS
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        sentence = ""
        for part in re.split(r"(?<=[.!?])\s+", para):
            if len(sentence) + len(part) + 1 > max_chars and sentence:
                pieces.append(sentence.strip())
                sentence = part
            else:
                sentence = f"{sentence} {part}".strip()
        if sentence:
            pieces.append(sentence.strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(current) + len(piece) + 2 > max_chars and current:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def _post_tts(text: str, voice_id: str) -> bytes:
    response = requests.post(
        f"{config.ELEVENLABS_API_BASE}/v1/text-to-speech/{voice_id}",
        headers=_headers("audio/mpeg"),
        params={"output_format": config.ELEVENLABS_OUTPUT_FORMAT},
        json={
            "text": text,
            "model_id": config.ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                # Narration, not performance: style drift makes a demo voice
                # wander in tone between chunks of the same take.
                "style": 0.0,
                "use_speaker_boost": True,
            },
        },
        timeout=config.ELEVENLABS_TIMEOUT_SECONDS,
    )
    _raise_for_elevenlabs(response)
    if not response.content:
        raise RuntimeError("ElevenLabs returned an empty audio body")
    return response.content


def synthesize(text: str, *, voice_id: str | None = None,
               input_ref: str | None = None) -> bytes | None:
    """Narration in, MP3 bytes out. None on any failure, with the reason logged.

    A partially synthesized take is not returned: a voiceover missing its
    middle paragraph sounds correct and is wrong, which is the exact failure
    mode this project spends its ground rules avoiding.
    """
    if not text.strip():
        return None
    if not configured():
        log_eval(STAGE, input_ref, False, error_message="ELEVENLABS_API_KEY missing")
        print("      [warn] ELEVENLABS_API_KEY missing - script written, no audio")
        return None

    voice_id = voice_id or resolve_voice()
    chunks = split_for_tts(text)
    audio = bytearray()

    for index, chunk in enumerate(chunks, start=1):
        ref = input_ref if len(chunks) == 1 else f"{input_ref}#{index}"
        part, ok = call_external_api(_post_tts, chunk, voice_id,
                                     stage=STAGE, input_ref=ref)
        if not ok or not part:
            print(f"      [warn] speech failed on chunk {index}/{len(chunks)}, "
                  f"no audio written: {_last_error or 'see eval_log'}")
            return None
        audio.extend(part)

    return bytes(audio)
