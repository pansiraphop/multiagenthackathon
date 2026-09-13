"""Combine caption + transcript into the text extraction actually reads.

Whisper output is best-effort. This module scores transcript reliability with
deterministic heuristics (no LLM) so the agent can decide whether to trust it,
and builds a labeled SOURCE block for stage 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from lib.normalize import clean_source_text

Tier = Literal["caption_only", "caption_plus_transcript", "transcript_primary"]

# Tokens that show a spoken recipe, not music/noise.
_RECIPE_CUES = frozenset({
    "add", "boil", "braise", "butter", "chop", "cook", "cream", "cup", "dice",
    "fry", "garlic", "gram", "heat", "ingredient", "minutes", "mix", "oil",
    "onion", "oven", "pan", "pasta", "pepper", "pinch", "recipe", "roast",
    "salt", "sauce", "saute", "sear", "simmer", "spoon", "stir", "tablespoon",
    "teaspoon", "tomato", "until", "water",
})

_CAPTION_SECTION = re.compile(
    r"\b(ingredients?|how to make|method|directions|instructions|steps?)\b",
    re.I,
)


@dataclass(frozen=True)
class SourceDecision:
    """What extraction should read, and why."""

    tier: Tier
    transcript_score: float          # 0.0 – 1.0
    caption_score: float             # 0.0 – 1.0
    source_text: str
    reasons: tuple[str, ...]
    caption_chars: int
    transcript_chars: int

    @property
    def use_transcript(self) -> bool:
        return self.tier != "caption_only"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z]{3,}", text.lower())}


def score_caption(caption: str | None) -> tuple[float, list[str]]:
    """How usable the Instagram caption is as a recipe source."""
    reasons: list[str] = []
    if not caption or not caption.strip():
        return 0.0, ["caption missing"]

    text = caption.strip()
    n = len(text)
    tokens = _tokens(text)
    cue_hits = len(tokens & _RECIPE_CUES)
    has_section = bool(_CAPTION_SECTION.search(text))

    score = 0.0
    if n >= 80:
        score += 0.35
        reasons.append(f"caption length {n}")
    elif n >= 30:
        score += 0.15
        reasons.append(f"thin caption length {n}")
    else:
        reasons.append(f"very short caption ({n})")

    if has_section:
        score += 0.35
        reasons.append("caption has ingredients/method section")
    if cue_hits >= 4:
        score += 0.30
        reasons.append(f"caption recipe cues={cue_hits}")
    elif cue_hits >= 1:
        score += 0.15
        reasons.append(f"few caption recipe cues={cue_hits}")

    return min(score, 1.0), reasons


def score_transcript(
    transcript: str | None,
    caption: str | None = None,
) -> tuple[float, list[str]]:
    """How reliable the Whisper transcript looks for recipe extraction.

    High score means include it. Low score means discard it and lean on the
    caption — music-heavy reels routinely produce fluent junk.
    """
    reasons: list[str] = []
    if not transcript or not transcript.strip():
        return 0.0, ["transcript missing"]

    text = transcript.strip()
    n = len(text)
    tokens = _tokens(text)
    if not tokens:
        return 0.0, ["transcript has no words"]

    cue_hits = len(tokens & _RECIPE_CUES)
    score = 0.0

    if n >= 200:
        score += 0.25
        reasons.append(f"transcript length {n}")
    elif n >= 60:
        score += 0.10
        reasons.append(f"short transcript length {n}")
    else:
        reasons.append(f"too short to trust ({n} chars)")
        return min(score, 0.2), reasons

    if cue_hits >= 6:
        score += 0.40
        reasons.append(f"transcript recipe cues={cue_hits}")
    elif cue_hits >= 3:
        score += 0.25
        reasons.append(f"moderate transcript recipe cues={cue_hits}")
    else:
        score += 0.05
        reasons.append(f"few transcript recipe cues={cue_hits}")

    if caption and caption.strip():
        cap_tokens = _tokens(caption)
        if cap_tokens:
            overlap = len(tokens & cap_tokens) / len(cap_tokens)
            score += min(0.35, overlap)
            reasons.append(f"caption token overlap={overlap:.0%}")
    else:
        # No caption to check against — lean harder on cue density.
        if cue_hits >= 8:
            score += 0.20
            reasons.append("no caption; dense recipe speech")

    return min(score, 1.0), reasons


def decide_source(
    caption: str | None,
    transcript: str | None,
) -> SourceDecision:
    """Pick caption / transcript mix and build the extraction SOURCE text."""
    cap_score, cap_reasons = score_caption(caption)
    tr_score, tr_reasons = score_transcript(transcript, caption)
    reasons = tuple(cap_reasons + tr_reasons)

    caption_clean = (caption or "").strip() or None
    transcript_clean = (transcript or "").strip() or None

    if tr_score < 0.35 or not transcript_clean:
        tier: Tier = "caption_only"
        if not caption_clean and transcript_clean:
            # Nothing else to offer — take the weak transcript.
            tier = "transcript_primary"
        parts = [f"CAPTION:\n{caption_clean}"] if caption_clean else []
        if tier == "transcript_primary" and transcript_clean:
            parts = [f"TRANSCRIPT:\n{transcript_clean}"]
    elif cap_score < 0.35 and tr_score >= 0.35:
        tier = "transcript_primary"
        parts = []
        if caption_clean:
            parts.append(f"CAPTION:\n{caption_clean}")
        parts.append(f"TRANSCRIPT:\n{transcript_clean}")
    else:
        tier = "caption_plus_transcript"
        parts = []
        if caption_clean:
            parts.append(f"CAPTION:\n{caption_clean}")
        if transcript_clean:
            parts.append(f"TRANSCRIPT:\n{transcript_clean}")

    if not parts:
        raise ValueError("no caption or transcript available for extraction")

    source_text = clean_source_text("\n\n".join(parts))
    return SourceDecision(
        tier=tier,
        transcript_score=tr_score,
        caption_score=cap_score,
        source_text=source_text,
        reasons=reasons,
        caption_chars=len(caption_clean or ""),
        transcript_chars=len(transcript_clean or ""),
    )


def rank_pending(rows: list[dict[str, Any]]) -> list[tuple[float, SourceDecision, dict]]:
    """Rank pending recipes for an agent choosing what to extract next.

    Prefer rich captions; a reliable transcript is a bonus, never required.
    """
    ranked: list[tuple[float, SourceDecision, dict]] = []
    for row in rows:
        try:
            decision = decide_source(row.get("raw_caption"), row.get("raw_transcript"))
        except ValueError:
            continue
        priority = (
            decision.caption_score * 0.7
            + decision.transcript_score * 0.3
            + min(decision.caption_chars, 800) / 8000
        )
        ranked.append((priority, decision, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


if __name__ == "__main__":
    # Caption-only thin music reel
    junk = decide_source(
        "Katsu Curry recipe with ingredients listed",
        "girl arrangement",
    )
    assert junk.tier == "caption_only", junk
    assert junk.transcript_score < 0.35, junk.transcript_score

    # Voiceover reel like the rigatoni DM
    good = decide_source(
        "Rigatoni alla vodka. Use cherry tomatoes and heavy cream.",
        "Today I'm gonna show you how to make rigatoni. Add onion and garlic "
        "to the pan with olive oil and salt. Fry tomato paste, add vodka, "
        "then cherry tomatoes. Boil pasta, blend the sauce, add cream, "
        "butter and parmesan.",
    )
    assert good.use_transcript, good
    assert good.transcript_score >= 0.35, good.transcript_score

    thin_cap = decide_source(
        "save this!!",
        "Cook onion and garlic in oil. Add tomato paste and cream. "
        "Boil pasta until al dente. Stir in butter and salt.",
    )
    assert thin_cap.tier == "transcript_primary", thin_cap

    print("source: all self-tests passed")
