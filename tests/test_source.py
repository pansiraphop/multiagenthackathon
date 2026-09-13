"""Unit tests for caption/transcript reliability and source selection."""

from __future__ import annotations

import unittest

from lib.source import decide_source, rank_pending, score_transcript


class TranscriptReliability(unittest.TestCase):
    def test_music_junk_is_rejected(self) -> None:
        score, reasons = score_transcript("girl arrangement", "Katsu Curry ingredients")
        self.assertLess(score, 0.35)
        decision = decide_source(
            "Katsu Curry\n\nIngredients\nChicken\nFlour\nEgg",
            "girl arrangement",
        )
        self.assertEqual(decision.tier, "caption_only")
        self.assertFalse(decision.use_transcript)

    def test_voiceover_is_accepted(self) -> None:
        transcript = (
            "Today I'm gonna show you how to make rigatoni. Add onion and garlic "
            "to the pan with olive oil and a pinch of salt. Fry tomato paste, "
            "add the vodka, then cherry tomatoes. Boil the pasta, blend the "
            "sauce, add heavy cream, butter and cheese."
        )
        caption = (
            "Rigatoni alla vodka like a pro!\n\nMaking a restaurant-quality "
            "rigatoni alla vodka at home. Use cherry tomatoes and heavy cream."
        )
        score, _ = score_transcript(transcript, caption)
        self.assertGreaterEqual(score, 0.35)
        decision = decide_source(caption, transcript)
        self.assertIn(decision.tier, {"caption_plus_transcript", "transcript_primary"})
        self.assertTrue(decision.use_transcript)
        self.assertIn("CAPTION:", decision.source_text)
        self.assertIn("TRANSCRIPT:", decision.source_text)

    def test_thin_caption_prefers_transcript(self) -> None:
        decision = decide_source(
            "save this!!",
            "Cook onion and garlic in oil. Add tomato paste and cream. "
            "Boil pasta until soft. Stir in butter and salt.",
        )
        self.assertEqual(decision.tier, "transcript_primary")


class RankPending(unittest.TestCase):
    def test_rich_caption_ranks_first(self) -> None:
        rows = [
            {
                "id": "thin",
                "raw_caption": "yum",
                "raw_transcript": None,
                "source_url": "https://instagram.com/reel/thin",
            },
            {
                "id": "rich",
                "raw_caption": (
                    "Ingredients\nChicken\nGarlic\nOil\nSalt\n\n"
                    "How to make\nCook the chicken with garlic in oil."
                ),
                "raw_transcript": None,
                "source_url": "https://instagram.com/reel/rich",
            },
        ]
        ranked = rank_pending(rows)
        self.assertEqual(ranked[0][2]["id"], "rich")


if __name__ == "__main__":
    unittest.main()
