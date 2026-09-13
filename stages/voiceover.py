"""Stage 7 — voiceover: the week, and each meal, as something you can listen to.

A separate workflow rather than a seventh link in the chain. It reads what
stage 1 extracted and what stages 3 and 6 scheduled, decides what is worth
saying out loud, writes the narration, and has ElevenLabs speak it. Nothing
downstream depends on the result, so this can fail without touching the plan —
which is exactly why it's safe to run at 3 PM.

    python -m stages.voiceover                 # the week: script + MP3
    python -m stages.voiceover --script-only   # no audio, no ElevenLabs key needed
    python -m stages.voiceover --meal <id>     # cook-along for one meal_plan row
    python -m stages.voiceover --meal all      # ...one file per scheduled meal
    python -m stages.voiceover --voice Adam    # a voice name or a voice id
    python -m stages.voiceover --voices        # what this account can speak with
    python -m stages.voiceover --no-llm        # deterministic template script

Reads meal_plan, recipes, recipe_ingredients, cook_slots, shopping_list and
instacart_orders. Writes files under VOICEOVER_DIR and rows to eval_log —
no other table, so re-running is always safe.

Two fallbacks, because the interesting failure here is a silent one. If the
model call fails, a deterministic template script is used instead and says so.
If ElevenLabs fails, the script is still written to disk: you lose the audio,
never the words.

Idempotent: an MP3 that already exists is left alone unless --force. Speech
costs credits, and a demo gets re-run.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from lib import db, voice
from lib.evals import log_eval
from lib.llm import LLMFailure, call_llm_structured
from lib.normalize import render_amount
from lib.prompts import (
    COOK_VOICEOVER_PROMPT,
    VOICEOVER_SYSTEM,
    WEEK_VOICEOVER_PROMPT,
)
from lib.schemas import (
    VoiceoverScript,
    VoiceoverSegment,
    attended_minutes,
    spoken_seconds,
    spoken_text,
    validate_voiceover,
)

STAGE = "voiceover"


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(config.TIMEZONE)


def _clock(when: datetime) -> str:
    """'7:15 pm'. Speech models read this correctly; '19:15' they do not."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'am' if when.hour < 12 else 'pm'}"


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


# ---------------------------------------------------------------------------
# the brief — what the system actually decided
# ---------------------------------------------------------------------------

def week_brief(week_start: date) -> dict:
    """Everything worth narrating about one planned week.

    Assembled from rows only. This never imports another stage, so it keeps
    working when the planner changes and it can be run against hand-inserted
    rows before anything upstream exists.
    """
    week = week_start.isoformat()
    meals = sorted(db.select("meal_plan", "*", week_start_date=week),
                   key=lambda m: m["planned_start_time"])
    recipes = {r["id"]: r for r in db.successful_recipes()}
    slots = db.select("cook_slots", "*", week_start_date=week)
    orders = db.select("instacart_orders", "*", week_start_date=week)
    order = orders[0] if orders else None

    planned = []
    for meal in meals:
        recipe = recipes.get(meal["recipe_id"])
        if not recipe:
            continue
        start = _local(meal["planned_start_time"])
        slot = next((s for s in slots if s["id"] == meal.get("cook_slot_id")), None)
        planned.append({
            "meal_id": meal["id"],
            "title": recipe.get("title") or "an untitled recipe",
            "cuisine": recipe.get("cuisine"),
            "day": f"{start:%A}",
            "time": _clock(start),
            "attended": attended_minutes(recipe),
            "window": slot["duration_minutes"] if slot else None,
            "reason": meal.get("score_reason"),
            "reconstructed": recipe.get("provenance") == "reconstructed",
            "scheduled": bool(meal.get("calendar_event_id")),
        })

    # Days the planner passed over, and why. Proving the system SKIPS evenings
    # is harder and more convincing than proving it fills them.
    cooked_on = {item["day"] for item in planned}
    no_window, nothing_fitted = [], []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        name = f"{day:%A}"
        if name in cooked_on:
            continue
        on_that_day = [s for s in slots if _local(s["slot_start"]).date() == day]
        (nothing_fitted if on_that_day else no_window).append(name)

    longest = max((s["duration_minutes"] for s in slots), default=0)
    used = {m["recipe_id"] for m in meals}
    too_long = [
        {"title": r.get("title") or "an untitled recipe",
         "minutes": attended_minutes(r)}
        for r in recipes.values()
        if r["id"] not in used
        and longest
        and attended_minutes(r) + config.SLOT_BUFFER_MINUTES > longest
    ]

    shopping = db.select("shopping_list", "*", week_start_date=week)
    delivery = None
    if order and order.get("delivery_window_end") and planned:
        ends = _local(order["delivery_window_end"])
        first = _local(meals[0]["planned_start_time"])
        delivery = {
            "day": f"{ends:%A}",
            "time": _clock(ends),
            "hours_before_first_meal": round(
                (first - ends).total_seconds() / 3600, 1),
            "first_meal": planned[0]["title"],
        }

    return {
        "week_start": week_start,
        "meals": planned,
        "no_window": no_window,
        "nothing_fitted": nothing_fitted,
        "too_long": too_long,
        "recipes_available": len(recipes),
        "items_to_buy": len(shopping),
        "cart_built": bool(order and order.get("method")
                           and order["method"] != "fallback_links"),
        "delivery": delivery,
    }


def format_week_brief(brief: dict) -> str:
    """The brief as prose for the model. Facts only — no instructions here."""
    lines = [
        f"Week beginning Monday {brief['week_start']:%d %B}.",
        f"{brief['recipes_available']} recipes were available from saved reels; "
        f"{len(brief['meals'])} were scheduled.",
        "",
        "MEALS SCHEDULED:",
    ]
    for item in brief["meals"] or [{}]:
        if not item:
            lines.append("- none")
            break
        window = (f", in a {item['window']}-minute gap" if item["window"] else "")
        lines.append(
            f"- {item['title']} ({item['cuisine'] or 'unknown cuisine'}) on "
            f"{item['day']} at {item['time']}, {item['attended']} minutes of "
            f"cooking{window}."
        )
        if item["reason"]:
            lines.append(f"  Why then: {item['reason']}")
        if item["reconstructed"]:
            lines.append("  This reel contained no actual recipe; it was "
                         "reconstructed from the dish name and web research, "
                         "and is labelled as such in the calendar.")

    if brief["no_window"]:
        lines += ["", "DAYS WITH NO FREE EVENING AT ALL: "
                  + ", ".join(brief["no_window"]) + "."]
    if brief["nothing_fitted"]:
        lines += ["", "DAYS WITH A FREE EVENING THAT NOTHING FITTED INTO: "
                  + ", ".join(brief["nothing_fitted"]) + "."]
    for item in brief["too_long"]:
        lines.append(f"- {item['title']} needs {item['minutes']} minutes and was "
                     f"too long for any window this week, so it was left out.")

    lines += ["", "GROCERIES:"]
    if brief["items_to_buy"]:
        built = ("A real Instacart cart was built." if brief["cart_built"]
                 else "No cart could be built automatically, so search links "
                      "were produced instead.")
        lines.append(f"- {brief['items_to_buy']} items were missing from the "
                     f"pantry and need buying. {built}")
    else:
        lines.append("- nothing recorded yet.")
    if brief["delivery"]:
        d = brief["delivery"]
        lines.append(
            f"- Delivery lands {d['day']} by {d['time']}, "
            f"{d['hours_before_first_meal']} hours before {d['first_meal']} is "
            f"due to be cooked."
        )
    return "\n".join(lines)


def _spoken_ingredient(ing: dict) -> str:
    """'olive oil - about 2 tbsp (a good glug)'.

    Same amounts as the calendar invite, with the tilde spelled out: a voice
    reads "~2 tbsp" as "two tablespoons" and the cook loses the one signal
    telling them it was an estimate.
    """
    quantity = ing.get("quantity")
    amount = render_amount({
        "quantity": float(quantity) if quantity is not None else None,
        "unit": ing.get("unit"),
        "qualitative_note": ing.get("qualitative_note"),
        "is_approximate": ing.get("is_approximate"),
    })
    return f"{ing['name']} - {amount.replace('~', 'about ')}"


def meal_brief(meal_id: str) -> dict | None:
    """One scheduled meal, everything the cook needs read aloud."""
    rows = db.select("meal_plan", "*", id=meal_id)
    if not rows:
        return None
    meal = rows[0]
    recipe = db.get_recipe_with_ingredients(meal["recipe_id"])
    if not recipe:
        return None

    start = _local(meal["planned_start_time"])
    return {
        "meal_id": meal["id"],
        "week_start_date": meal["week_start_date"],
        "title": recipe.get("title") or "an untitled recipe",
        "day": f"{start:%A}",
        "time": _clock(start),
        "attended": attended_minutes(recipe),
        "servings": recipe.get("servings"),
        "reconstructed": recipe.get("provenance") == "reconstructed",
        "ingredients": [_spoken_ingredient(i) for i in recipe.get("ingredients", [])],
        "steps": list(recipe.get("steps") or []),
    }


def format_meal_brief(brief: dict) -> str:
    lines = [
        f"Dish: {brief['title']}",
        f"Cooking time: {brief['attended']} minutes"
        + (f", serves {brief['servings']}" if brief["servings"] else ""),
    ]
    if brief["reconstructed"]:
        lines.append("Note: the reel did not contain a recipe, so this was "
                     "reconstructed from research. Say so once, early, in one "
                     "short sentence.")
    lines += ["", "INGREDIENTS:"]
    lines += [f"- {line}" for line in brief["ingredients"]] or ["- none recorded"]
    lines += ["", "STEPS:"]
    lines += [f"{n}. {step}" for n, step in enumerate(brief["steps"], start=1)] \
        or ["1. no method recorded"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the script — model first, template always available
# ---------------------------------------------------------------------------

def template_week_script(brief: dict) -> VoiceoverScript:
    """Deterministic fallback. Flatter than the model, never fails, never lies."""
    segments = [VoiceoverSegment(
        label="opening",
        text=f"Here is your week. {len(brief['meals'])} meals, fitted around "
             f"the evenings you were actually free.",
    )]
    for item in brief["meals"]:
        reason = f" {item['reason']}" if item["reason"] else ""
        segments.append(VoiceoverSegment(
            label=_slug(item["day"]),
            text=f"{item['day']} at {item['time']}, {item['title']}. "
                 f"{item['attended']} minutes.{reason}",
        ))
    if brief["no_window"]:
        segments.append(VoiceoverSegment(
            label="skipped",
            text=f"Nothing on {', '.join(brief['no_window'])}. There was no "
                 f"free evening to cook in.",
        ))
    if brief["delivery"]:
        d = brief["delivery"]
        segments.append(VoiceoverSegment(
            label="delivery",
            text=f"The groceries land {d['day']} by {d['time']}, "
                 f"{d['hours_before_first_meal']} hours before you cook.",
        ))
    return VoiceoverScript(title=f"week-of-{brief['week_start']}",
                           segments=segments)


def template_cook_script(brief: dict) -> VoiceoverScript:
    segments = [VoiceoverSegment(
        label="opening",
        text=f"Tonight you are making {brief['title']}. "
             f"About {brief['attended']} minutes.",
    )]
    if brief["ingredients"]:
        segments.append(VoiceoverSegment(
            label="ingredients",
            text="You will need: " + "; ".join(brief["ingredients"]) + ".",
        ))
    for index, step in enumerate(brief["steps"], start=1):
        segments.append(VoiceoverSegment(label=f"step-{index}",
                                         text=f"Step {index}. {step}"))
    segments.append(VoiceoverSegment(label="close", text="That's it. Enjoy."))
    return VoiceoverScript(title=_slug(brief["title"]), segments=segments)


def compose(
    prompt: str,
    *,
    fallback: VoiceoverScript,
    must_mention: tuple[str, ...],
    max_words: int,
    input_ref: str,
    use_llm: bool = True,
) -> tuple[VoiceoverScript, bool]:
    """Return (script, written_by_model).

    The validator feeds real problems back — "never mentions Thursday's meal",
    "320 words, cut it" — which is the whole reason the retry loop is worth
    having. When it still can't get there, the template ships.
    """
    if not use_llm:
        return fallback, False
    try:
        script = call_llm_structured(
            prompt,
            VoiceoverScript,
            stage=STAGE,
            input_ref=input_ref,
            system=VOICEOVER_SYSTEM,
            validate=lambda s: validate_voiceover(
                s, must_mention=must_mention, max_words=max_words),
        )
    except LLMFailure as exc:
        print(f"      [warn] script generation failed ({exc}) - using the template")
        return fallback, False
    return script, True


def script_markdown(script: VoiceoverScript, brief_text: str,
                    by_model: bool) -> str:
    """The script file. Segment labels are headings here and silent in the audio."""
    spoken = spoken_text(script)
    head = [
        f"# {script.title}",
        "",
        f"_{len(spoken.split())} words, about {spoken_seconds(spoken)}s spoken. "
        f"{'Written by the model' if by_model else 'Deterministic template'}._",
        "",
    ]
    body = []
    for segment in script.segments:
        body += [f"## {segment.label}", "", segment.text.strip(), ""]
    return "\n".join(head + body + ["---", "", "<details><summary>Brief the "
                                    "script was written from</summary>", "",
                                    "```", brief_text, "```", "", "</details>"])


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def out_dir(week_start: date) -> Path:
    path = Path(config.VOICEOVER_DIR) / week_start.isoformat()
    path.mkdir(parents=True, exist_ok=True)
    return path


def guidance_dir(meal_id: str, week_start: date | str) -> Path:
    """Per-meal folder for step-by-step audio the cook page advances through."""
    week = week_start if isinstance(week_start, str) else week_start.isoformat()
    path = Path(config.VOICEOVER_DIR) / week / f"cook-{meal_id[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cook_guidance(meal_id: str) -> dict | None:
    """Segment list for the /cook page Next-button flow.

    Uses the deterministic cook script: the page needs something instantly and
    reliably, and a model rewrite would make "Next" wait on a second LLM call
    with oily hands. Audio is synthesized lazily by ensure_segment_audio().
    """
    brief = meal_brief(meal_id)
    if not brief or not brief["steps"]:
        return None

    script = template_cook_script(brief)
    segments = []
    for index, segment in enumerate(script.segments):
        step_index = None
        if segment.label.startswith("step-"):
            try:
                step_index = int(segment.label.split("-", 1)[1]) - 1
            except ValueError:
                step_index = None
        segments.append({
            "index": index,
            "label": segment.label,
            "text": segment.text.strip(),
            "step_index": step_index,
            "audio_url": f"/cook/{meal_id}/audio/{index}",
        })

    return {
        "meal_id": meal_id,
        "title": brief["title"],
        "week_start_date": brief["week_start_date"],
        "segments": segments,
    }


def ensure_segment_audio(meal_id: str, index: int, *,
                         force: bool = False) -> Path | None:
    """Speak one guidance segment. Skip-if-present; return None on TTS failure."""
    guidance = cook_guidance(meal_id)
    if not guidance or index < 0 or index >= len(guidance["segments"]):
        return None

    segment = guidance["segments"][index]
    directory = guidance_dir(meal_id, guidance["week_start_date"])
    path = directory / f"{index:02d}-{_slug(segment['label'])}.mp3"
    if path.exists() and not force:
        return path

    audio = voice.synthesize(segment["text"], input_ref=f"cook:{meal_id}:{index}")
    if not audio:
        return None
    path.write_bytes(audio)
    return path


def render(script: VoiceoverScript, audio_path: Path, *,
           voice_id: str | None, force: bool) -> Path | None:
    """Speak the script. Skip-if-present, because speech costs credits."""
    if audio_path.exists() and not force:
        print(f"      {audio_path} already exists - pass --force to re-speak it")
        return audio_path

    audio = voice.synthesize(spoken_text(script), voice_id=voice_id,
                             input_ref=audio_path.stem)
    if not audio:
        return None

    audio_path.write_bytes(audio)
    print(f"      spoke  {audio_path}  ({len(audio) // 1024} KB)")
    return audio_path


def _emit(script: VoiceoverScript, brief_text: str, by_model: bool, *,
          stem: str, week_start: date, script_only: bool,
          voice_id: str | None, force: bool) -> dict:
    directory = out_dir(week_start)
    script_path = directory / f"{stem}.md"
    script_path.write_text(script_markdown(script, brief_text, by_model),
                           encoding="utf-8")

    spoken = spoken_text(script)
    print(f"      wrote  {script_path}  ({len(spoken.split())} words, "
          f"~{spoken_seconds(spoken)}s)")

    audio_path = None
    if not script_only:
        audio_path = render(script, directory / f"{stem}.mp3",
                            voice_id=voice_id, force=force)

    # A script with no audio is a degraded success, not a failure: the words
    # are on disk. Recorded as such so the brief can count it honestly.
    log_eval(STAGE, stem, True,
             error_message=None if (audio_path or script_only)
             else "script written, speech unavailable")

    return {"script": script, "script_path": script_path,
            "audio_path": audio_path, "by_model": by_model}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_week(week_start: date, *, use_llm: bool = True, script_only: bool = False,
             voice_id: str | None = None, force: bool = False) -> dict | None:
    brief = week_brief(week_start)
    if not brief["meals"]:
        print("[7] voiceover     no scheduled meals for this week - "
              "run the planner first")
        log_eval(STAGE, week_start.isoformat(), False,
                 error_message="no meal_plan rows to narrate")
        return None

    unwritten = sum(1 for m in brief["meals"] if not m["scheduled"])
    print(f"[7] voiceover     week of {week_start} | {len(brief['meals'])} meals"
          + (f", {unwritten} not yet on the calendar" if unwritten else "")
          + (" | script only" if script_only else ""))

    brief_text = format_week_brief(brief)
    script, by_model = compose(
        WEEK_VOICEOVER_PROMPT.format(
            brief=brief_text,
            target_words=config.WEEK_SCRIPT_TARGET_WORDS,
            max_words=config.WEEK_SCRIPT_MAX_WORDS,
        ),
        fallback=template_week_script(brief),
        # Every scheduled meal has to survive into the narration. A script that
        # drops Thursday sounds perfect and is wrong.
        must_mention=tuple(m["title"] for m in brief["meals"]),
        max_words=config.WEEK_SCRIPT_MAX_WORDS,
        input_ref=f"week:{week_start}",
        use_llm=use_llm,
    )
    return _emit(script, brief_text, by_model, stem="week",
                 week_start=week_start, script_only=script_only,
                 voice_id=voice_id, force=force)


def run_meal(meal_id: str, *, use_llm: bool = True, script_only: bool = False,
             voice_id: str | None = None, force: bool = False) -> dict | None:
    brief = meal_brief(meal_id)
    if not brief:
        print(f"[7] voiceover     no meal_plan row {meal_id}")
        log_eval(STAGE, meal_id, False, error_message="meal not found")
        return None
    if not brief["steps"]:
        print(f"[7] voiceover     {brief['title']} has no steps to read out")
        log_eval(STAGE, meal_id, False, error_message="recipe has no steps")
        return None

    print(f"[7] voiceover     cook-along | {brief['title']} | "
          f"{len(brief['steps'])} steps")

    brief_text = format_meal_brief(brief)
    script, by_model = compose(
        COOK_VOICEOVER_PROMPT.format(
            brief=brief_text, max_words=config.COOK_SCRIPT_MAX_WORDS),
        fallback=template_cook_script(brief),
        must_mention=(brief["title"],),
        max_words=config.COOK_SCRIPT_MAX_WORDS,
        input_ref=f"meal:{meal_id}",
        use_llm=use_llm,
    )

    return _emit(script, brief_text, by_model,
                 stem=f"cook-{_slug(brief['title'])[:40]}-{meal_id[:8]}",
                 week_start=date.fromisoformat(brief["week_start_date"]),
                 script_only=script_only, voice_id=voice_id, force=force)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="week start (YYYY-MM-DD)")
    parser.add_argument("--meal", metavar="ID",
                        help="cook-along for one meal_plan id, or 'all'")
    parser.add_argument("--script-only", action="store_true",
                        help="write the script and skip ElevenLabs entirely")
    parser.add_argument("--no-llm", action="store_true",
                        help="use the deterministic template script")
    parser.add_argument("--voice", help="ElevenLabs voice name or id")
    parser.add_argument("--voices", action="store_true",
                        help="list the voices on this account and exit")
    parser.add_argument("--force", action="store_true",
                        help="re-speak even if the MP3 already exists")
    args = parser.parse_args()

    if args.voices:
        voices = voice.available_voices()
        if not voices:
            print("no voices returned - the key is missing or lacks the "
                  "voices_read permission. --voice still takes an id, or one "
                  f"of: {', '.join(sorted(voice.DEFAULT_VOICES))}")
            return 1
        for item in voices:
            print(f"{item['voice_id']}  {item['name']}  "
                  f"({item.get('category', '?')})")
        return 0

    week_start = date.fromisoformat(args.week) if args.week else config.week_start()
    voice_id = voice.resolve_voice(args.voice) if not args.script_only else None
    common = {"use_llm": not args.no_llm, "script_only": args.script_only,
              "voice_id": voice_id, "force": args.force}

    if args.meal == "all":
        meals = db.select("meal_plan", "id", week_start_date=week_start.isoformat())
        if not meals:
            print(f"[7] voiceover     no meals planned for {week_start}")
            return 1
        return 0 if all(run_meal(m["id"], **common) for m in meals) else 1
    if args.meal:
        return 0 if run_meal(args.meal, **common) else 1
    return 0 if run_week(week_start, **common) else 1


if __name__ == "__main__":
    sys.exit(main())
