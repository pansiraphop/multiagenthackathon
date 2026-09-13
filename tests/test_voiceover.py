"""Stage 7 tests — the brief, the script validator, and the TTS chunker.

Nothing here touches ElevenLabs or Anthropic. The brief is pure given rows,
validation is pure, and the one external path is asserted to degrade rather
than raise. A failure here means the narration logic changed, not that an API
was slow.

    python -m tests.test_voiceover
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import config
from lib import voice
from lib.schemas import (
    VoiceoverScript,
    VoiceoverSegment,
    mentions,
    spoken_text,
    validate_voiceover,
)
from stages.voiceover import (
    format_week_brief,
    meal_brief,
    template_cook_script,
    template_week_script,
    week_brief,
)

WEEK = date(2026, 9, 14)                       # a Monday
MONDAY_6PM = datetime(2026, 9, 14, 18, 0, tzinfo=config.TIMEZONE)


def meal(**over) -> dict:
    base = {
        "id": "meal-1",
        "recipe_id": "r1",
        "cook_slot_id": "slot-1",
        "week_start_date": WEEK.isoformat(),
        "planned_start_time": MONDAY_6PM.isoformat(),
        "planned_end_time": (MONDAY_6PM + timedelta(minutes=35)).isoformat(),
        "score_reason": "Uses the spinach expiring Wednesday.",
        "calendar_event_id": "gcal-1",
        "status": "scheduled",
    }
    base.update(over)
    return base


def recipe(**over) -> dict:
    base = {
        "id": "r1",
        "title": "Weeknight Palak Paneer",
        "cuisine": "indian",
        "est_time_minutes": 35,
        "total_time_minutes": 35,
        "advance_prep_minutes": 0,
        "servings": 4,
        "steps": ["Blanch the spinach.", "Fry the paneer."],
        "provenance": "transcript",
        "ingredients": [
            {"name": "spinach", "quantity": 400, "unit": "g",
             "is_approximate": False, "qualitative_note": None},
            {"name": "ghee", "quantity": 2, "unit": "tbsp",
             "is_approximate": True, "qualitative_note": "a good glug"},
        ],
    }
    base.update(over)
    return base


def slot(day_offset: int = 0, **over) -> dict:
    start = MONDAY_6PM + timedelta(days=day_offset)
    base = {
        "id": "slot-1",
        "week_start_date": WEEK.isoformat(),
        "slot_start": start.isoformat(),
        "slot_end": (start + timedelta(minutes=90)).isoformat(),
        "duration_minutes": 90,
        "suitability_score": 0.8,
        "assigned": True,
    }
    base.update(over)
    return base


def order(**over) -> dict:
    base = {
        "id": "order-1",
        "week_start_date": WEEK.isoformat(),
        "cart_url": "https://instacart.com/cart/xyz",
        "item_count": 12,
        "unresolved_item_count": 0,
        "method": "browserbase",
        "delivery_window_start": (MONDAY_6PM - timedelta(hours=8)).isoformat(),
        "delivery_window_end": (MONDAY_6PM - timedelta(hours=6)).isoformat(),
    }
    base.update(over)
    return base


def brief_from(meals, recipes, slots=(), orders=(), shopping=()) -> dict:
    tables = {
        "meal_plan": list(meals),
        "cook_slots": list(slots),
        "instacart_orders": list(orders),
        "shopping_list": list(shopping),
    }
    with patch("lib.db.select", side_effect=lambda t, *_a, **_k: tables.get(t, [])), \
         patch("lib.db.successful_recipes", return_value=list(recipes)):
        return week_brief(WEEK)


class WeekBrief(unittest.TestCase):
    def test_meals_are_described_in_spoken_terms(self) -> None:
        item = brief_from([meal()], [recipe()], [slot()])["meals"][0]
        self.assertEqual(item["day"], "Monday")
        self.assertEqual(item["time"], "6:00 pm")
        self.assertEqual(item["attended"], 35)
        self.assertEqual(item["window"], 90)

    def test_attended_time_not_active_time(self) -> None:
        """A braise narrated as thirty minutes is the same bug as a braise
        scheduled into thirty minutes."""
        item = brief_from(
            [meal()], [recipe(est_time_minutes=30, total_time_minutes=150)],
            [slot()])["meals"][0]
        self.assertEqual(item["attended"], 150)

    def test_score_reason_carries_through(self) -> None:
        item = brief_from([meal()], [recipe()], [slot()])["meals"][0]
        self.assertIn("spinach", item["reason"])

    def test_meals_are_ordered_by_time(self) -> None:
        later = meal(id="meal-2", recipe_id="r2", cook_slot_id="slot-2",
                     planned_start_time=(MONDAY_6PM + timedelta(days=2)).isoformat())
        brief = brief_from([later, meal()],
                           [recipe(), recipe(id="r2", title="Chana Masala")],
                           [slot(), slot(day_offset=2, id="slot-2")])
        self.assertEqual([m["day"] for m in brief["meals"]],
                         ["Monday", "Wednesday"])

    def test_a_day_with_no_window_is_named(self) -> None:
        """Proving the planner skips evenings is the hardest thing to show."""
        brief = brief_from([meal()], [recipe()], [slot()])
        self.assertIn("Wednesday", brief["no_window"])
        self.assertNotIn("Monday", brief["no_window"])

    def test_a_free_evening_nothing_fitted_is_a_different_fact(self) -> None:
        brief = brief_from([meal()], [recipe()],
                           [slot(), slot(day_offset=2, id="slot-2",
                                         duration_minutes=30, assigned=False)])
        self.assertIn("Wednesday", brief["nothing_fitted"])
        self.assertNotIn("Wednesday", brief["no_window"])

    def test_recipes_too_long_for_the_week_are_reported(self) -> None:
        brief = brief_from(
            [meal()],
            [recipe(), recipe(id="r2", title="Short Rib Ragu",
                              total_time_minutes=240)],
            [slot()])
        self.assertEqual(brief["too_long"][0]["title"], "Short Rib Ragu")

    def test_an_unplanned_recipe_that_fits_is_not_called_too_long(self) -> None:
        brief = brief_from(
            [meal()], [recipe(), recipe(id="r2", title="Cacio e Pepe",
                                        total_time_minutes=20)], [slot()])
        self.assertEqual(brief["too_long"], [])

    def test_delivery_is_measured_against_the_first_meal(self) -> None:
        brief = brief_from([meal()], [recipe()], [slot()], [order()])
        self.assertEqual(brief["delivery"]["hours_before_first_meal"], 6.0)
        self.assertEqual(brief["delivery"]["first_meal"], "Weeknight Palak Paneer")

    def test_no_order_is_not_a_delivery_claim(self) -> None:
        """Stage 5 is opt-in. Narrating a delivery that was never ordered is
        the worst thing this workflow could do."""
        self.assertIsNone(brief_from([meal()], [recipe()], [slot()])["delivery"])

    def test_fallback_links_are_not_called_a_cart(self) -> None:
        brief = brief_from([meal()], [recipe()], [slot()],
                           [order(method="fallback_links")],
                           [{"ingredient_name": "paneer"}])
        self.assertFalse(brief["cart_built"])
        self.assertIn("No cart could be built", format_week_brief(brief))

    def test_a_meal_without_a_successful_recipe_is_dropped(self) -> None:
        self.assertEqual(brief_from([meal(recipe_id="gone")], [recipe()])["meals"],
                         [])

    def test_empty_week_produces_an_empty_meal_list(self) -> None:
        self.assertEqual(brief_from([], [])["meals"], [])


class BriefText(unittest.TestCase):
    def text(self, **kwargs) -> str:
        return format_week_brief(brief_from(**kwargs))

    def test_names_the_dish_the_day_and_the_reason(self) -> None:
        text = self.text(meals=[meal()], recipes=[recipe()], slots=[slot()])
        self.assertIn("Weeknight Palak Paneer", text)
        self.assertIn("Monday at 6:00 pm", text)
        self.assertIn("Uses the spinach expiring Wednesday.", text)

    def test_reconstruction_is_disclosed_to_the_narrator(self) -> None:
        text = self.text(meals=[meal()], recipes=[recipe(provenance="reconstructed")],
                         slots=[slot()])
        self.assertIn("reconstructed", text)

    def test_transcript_recipes_carry_no_disclosure(self) -> None:
        text = self.text(meals=[meal()], recipes=[recipe()], slots=[slot()])
        self.assertNotIn("reconstructed", text)


class MealBrief(unittest.TestCase):
    def brief(self, r=None, m=None):
        with patch("lib.db.select", return_value=[m or meal()]), \
             patch("lib.db.get_recipe_with_ingredients", return_value=r or recipe()):
            return meal_brief("meal-1")

    def test_estimates_are_spoken_as_estimates(self) -> None:
        """'~2 tbsp' is read aloud as a measurement. 'about' is the only thing
        telling the cook it was a guess."""
        lines = self.brief()["ingredients"]
        self.assertIn("ghee - about 2 tbsp (a good glug)", lines)
        self.assertIn("spinach - 400 g", lines)

    def test_missing_meal_returns_none(self) -> None:
        with patch("lib.db.select", return_value=[]):
            self.assertIsNone(meal_brief("nope"))

    def test_missing_recipe_returns_none(self) -> None:
        with patch("lib.db.select", return_value=[meal()]), \
             patch("lib.db.get_recipe_with_ingredients", return_value=None):
            self.assertIsNone(meal_brief("meal-1"))


class TemplateScripts(unittest.TestCase):
    """The fallback has to be shippable on its own — it is what runs when the
    model call fails, which is precisely when nobody is watching."""

    def test_week_template_names_every_meal(self) -> None:
        brief = brief_from([meal()], [recipe()], [slot()])
        text = spoken_text(template_week_script(brief))
        self.assertIn("Weeknight Palak Paneer", text)
        self.assertIn("Monday at 6:00 pm", text)

    def test_week_template_is_speakable(self) -> None:
        brief = brief_from([meal()], [recipe()], [slot()], [order()])
        self.assertEqual(
            validate_voiceover(template_week_script(brief),
                               must_mention=("Weeknight Palak Paneer",),
                               max_words=config.WEEK_SCRIPT_MAX_WORDS,
                               min_words=0),
            [])

    def test_cook_template_reads_every_step_in_order(self) -> None:
        cook = {"title": "Palak Paneer", "attended": 35, "servings": 4,
                "reconstructed": False, "ingredients": ["spinach - 400 g"],
                "steps": ["Blanch the spinach.", "Fry the paneer."]}
        text = spoken_text(template_cook_script(cook))
        self.assertLess(text.index("Blanch"), text.index("Fry"))
        self.assertIn("Step 2 of 2.", text)
        self.assertIn("heat, timing", text.lower())

    def test_spoken_step_keeps_heat_and_spells_ranges(self) -> None:
        from stages.voiceover import _spoken_step
        line = _spoken_step(
            2, 5,
            "Warm the olive oil in a large skillet over medium heat for 8-10 min.")
        self.assertIn("Step 2 of 5.", line)
        self.assertIn("medium heat", line)
        self.assertIn("8 to 10 minutes", line)


class CookGuidance(unittest.TestCase):
    """What the /cook page Next button walks through."""

    def guidance(self, use_llm=False):
        from stages.voiceover import cook_guidance
        with patch("stages.voiceover.meal_brief", return_value={
            "meal_id": "meal-1",
            "week_start_date": WEEK.isoformat(),
            "title": "Weeknight Palak Paneer",
            "attended": 35,
            "servings": 4,
            "reconstructed": False,
            "ingredients": ["spinach - 400 g"],
            "steps": [
                "Blanch the spinach briefly in boiling water over high heat.",
                "Fry the paneer in ghee over medium heat until golden.",
            ],
            "source_caption": "",
            "source_transcript": "Cook the spinach, then fry the paneer on medium.",
        }), patch("stages.voiceover.guidance_dir",
                  return_value=Path("/tmp/instacook-voice-test")):
            Path("/tmp/instacook-voice-test").mkdir(parents=True, exist_ok=True)
            for stale in Path("/tmp/instacook-voice-test").glob("*"):
                stale.unlink()
            return cook_guidance("meal-1", use_llm=use_llm, force=True)

    def test_segments_cover_opening_ingredients_steps_and_close(self) -> None:
        labels = [s["label"] for s in self.guidance()["segments"]]
        self.assertEqual(labels[:2], ["opening", "ingredients"])
        self.assertEqual(labels[-1], "close")
        self.assertIn("step-1", labels)
        self.assertIn("step-2", labels)

    def test_step_segments_point_at_method_indices(self) -> None:
        by_label = {s["label"]: s for s in self.guidance()["segments"]}
        self.assertEqual(by_label["step-1"]["step_index"], 0)
        self.assertEqual(by_label["step-2"]["step_index"], 1)
        self.assertIsNone(by_label["opening"]["step_index"])

    def test_each_segment_has_an_audio_url(self) -> None:
        for seg in self.guidance()["segments"]:
            self.assertTrue(seg["audio_url"].endswith(f"/audio/{seg['index']}"))

    def test_template_guidance_keeps_heat_cues(self) -> None:
        steps = [s for s in self.guidance()["segments"]
                 if s["label"].startswith("step-")]
        self.assertIn("medium heat", steps[1]["text"])
        self.assertIn("high heat", steps[0]["text"])

    def test_missing_meal_returns_none(self) -> None:
        from stages.voiceover import cook_guidance
        with patch("stages.voiceover.meal_brief", return_value=None):
            self.assertIsNone(cook_guidance("nope"))


class FormatMealBrief(unittest.TestCase):
    def test_includes_source_transcript_for_detail(self) -> None:
        from stages.voiceover import format_meal_brief
        text = format_meal_brief({
            "title": "Rigatoni", "attended": 40, "servings": 4,
            "reconstructed": False,
            "ingredients": ["rigatoni - 500 g"],
            "steps": ["Warm oil over medium heat."],
            "source_caption": "Rigatoni like a pro",
            "source_transcript": "Every amazing sauce starts with onion and garlic "
                                 "over medium heat.",
        })
        self.assertIn("TRANSCRIPT:", text)
        self.assertIn("medium heat", text)
        self.assertIn("Warm oil over medium heat.", text)


class Validation(unittest.TestCase):
    def script(self, *texts) -> VoiceoverScript:
        return VoiceoverScript(
            title="t",
            segments=[VoiceoverSegment(label=str(i), text=t)
                      for i, t in enumerate(texts)])

    def test_clean_narration_passes(self) -> None:
        self.assertEqual(
            validate_voiceover(self.script("Monday you cook palak paneer."),
                               must_mention=("Palak Paneer",),
                               max_words=100, min_words=0),
            [])

    def test_a_dropped_meal_is_caught(self) -> None:
        """The failure a listener never notices: it sounds complete."""
        problems = validate_voiceover(
            self.script("Monday you cook palak paneer."),
            must_mention=("Palak Paneer", "Chana Masala"),
            max_words=100, min_words=0)
        self.assertTrue(any("Chana Masala" in p for p in problems))

    def test_markdown_is_rejected(self) -> None:
        problems = validate_voiceover(self.script("**Monday** you cook."),
                                      max_words=100, min_words=0)
        self.assertTrue(any("cannot read aloud" in p for p in problems))

    def test_overlong_scripts_are_rejected(self) -> None:
        problems = validate_voiceover(self.script("word " * 50),
                                      max_words=20, min_words=0)
        self.assertTrue(any("must be under" in p for p in problems))

    def test_empty_segment_is_rejected(self) -> None:
        problems = validate_voiceover(self.script("A real line here.", "   "),
                                      max_words=100, min_words=0)
        self.assertTrue(any("no text" in p for p in problems))

    def test_mentions_tolerates_natural_phrasing(self) -> None:
        self.assertTrue(mentions("then the palak paneer on Monday",
                                 "Weeknight Palak Paneer"))
        self.assertFalse(mentions("then the chana masala on Monday",
                                  "Weeknight Palak Paneer"))


class Chunking(unittest.TestCase):
    def test_short_text_is_one_request(self) -> None:
        self.assertEqual(voice.split_for_tts("Hello there."), ["Hello there."])

    def test_long_text_splits_on_paragraphs(self) -> None:
        text = "\n\n".join(["a" * 300] * 10)
        chunks = voice.split_for_tts(text, max_chars=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks).count("a"), 3000)

    def test_a_single_huge_paragraph_splits_on_sentences(self) -> None:
        text = " ".join(["This is a sentence."] * 200)
        chunks = voice.split_for_tts(text, max_chars=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertTrue(all(c.endswith(".") for c in chunks))

    def test_empty_text_makes_no_request(self) -> None:
        self.assertEqual(voice.split_for_tts("   "), [])


class Degradation(unittest.TestCase):
    """eval_log is mocked throughout: these assert the failure PATH, and a real
    Supabase write would make them network tests."""

    def setUp(self) -> None:
        for target in ("lib.voice.log_eval", "lib.external.log_eval"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_key_returns_none_instead_of_raising(self) -> None:
        """Ground rule 7. No audio is a degraded run; an exception is a dead one."""
        with patch.object(config, "ELEVENLABS_API_KEY", ""):
            self.assertIsNone(voice.synthesize("Anything at all."))

    def test_a_failed_request_returns_none(self) -> None:
        with patch.object(config, "ELEVENLABS_API_KEY", "sk_test"), \
             patch("lib.voice._post_tts", side_effect=RuntimeError("401")):
            self.assertIsNone(voice.synthesize("Anything at all.",
                                               voice_id="v1"))

    def test_partial_audio_is_never_returned(self) -> None:
        """A take missing its middle paragraph sounds right and is wrong."""
        calls = {"n": 0}

        def flaky(_text, _voice):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("rate limited")
            return b"\xff\xfb" * 10

        with patch.object(config, "ELEVENLABS_API_KEY", "sk_test"), \
             patch("lib.voice._post_tts", side_effect=flaky):
            audio = voice.synthesize("\n\n".join(["x" * 3000] * 3),
                                     voice_id="v1")
        self.assertIsNone(audio)


if __name__ == "__main__":
    unittest.main(verbosity=2)
