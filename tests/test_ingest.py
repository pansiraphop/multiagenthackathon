from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from lib.ingest import ReelIngest, ingest_reel, parse_reel_attachments
from lib.instagram import TextReply
from webhook import app


SAMPLE_PAYLOAD = {
    "object": "instagram",
    "entry": [
        {
            "messaging": [
                {
                    "sender": {"id": "2163155554635698"},
                    "message": {
                        "attachments": [
                            {
                                "type": "ig_reel",
                                "payload": {
                                    "reel_video_id": "17982850881065374",
                                    "title": " Katsu Curry ingredients and steps ",
                                    "url": (
                                        "https://www.instagram.com/reel/"
                                        "DdHHr2JMxDH/"
                                    ),
                                },
                            }
                        ]
                    },
                }
            ]
        }
    ],
}


class ParseReelsTest(unittest.TestCase):
    def test_parses_real_payload_shape(self) -> None:
        reels = parse_reel_attachments(SAMPLE_PAYLOAD)

        self.assertEqual(len(reels), 1)
        self.assertEqual(reels[0].sender_id, "2163155554635698")
        self.assertEqual(reels[0].reel_video_id, "17982850881065374")
        self.assertEqual(reels[0].caption, "Katsu Curry ingredients and steps")

    def test_ignores_non_instagram_payload(self) -> None:
        self.assertEqual(parse_reel_attachments({"object": "page"}), [])


class PersistReelTest(unittest.TestCase):
    @patch("lib.ingest.log_eval")
    @patch("lib.ingest.db.insert_recipe")
    @patch("lib.ingest.db.select", side_effect=[[], []])
    @patch("lib.ingest.config.INGEST_TRANSCRIBE", False)
    def test_caption_only_writes_pending_recipe(
        self,
        _select,
        insert_recipe,
        _log_eval,
    ) -> None:
        insert_recipe.return_value = {"id": "recipe-1"}
        reel = ReelIngest(
            sender_id="sender-1",
            reel_video_id="reel-1",
            source_url="https://www.instagram.com/reel/example/",
            caption="A complete caption",
        )

        row = ingest_reel(reel)

        self.assertEqual(row, {"id": "recipe-1"})
        insert_recipe.assert_called_once_with(
            source_url=reel.source_url,
            # Kept from the payload so stage 5b can DM this person back.
            sender_id="sender-1",
            raw_caption=reel.caption,
            raw_transcript=None,
            extraction_status="pending",
        )


class WebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    @patch("webhook.config.META_VERIFY_TOKEN", "verify-me")
    def test_verification_returns_plain_challenge(self) -> None:
        response = self.client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "12345",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "12345")

    @patch("webhook.ingest_reel")
    def test_delivery_queues_reel(self, ingest_reel_mock) -> None:
        response = self.client.post("/webhook", json=SAMPLE_PAYLOAD)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reels_queued"], 1)
        self.assertEqual(response.json()["replies_queued"], 0,
                         "a reel is not an answer to a follow-up question")
        ingest_reel_mock.assert_called_once()

    @patch("webhook.handle_reply")
    def test_delivery_queues_a_follow_up_reply(self, handle_reply_mock) -> None:
        """The return leg of the feedback loop arrives on the same endpoint."""
        response = self.client.post("/webhook", json={
            "object": "instagram",
            "entry": [{"messaging": [{"sender": {"id": "sender-1"},
                                      "message": {"mid": "m1", "text": "2"}}]}],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["replies_queued"], 1)
        self.assertEqual(response.json()["reels_queued"], 0)
        self.assertEqual(handle_reply_mock.call_args.args[0].text, "2")

    @patch("stages.followup.apply_reply", side_effect=RuntimeError("planner exploded"))
    def test_a_failed_reply_never_escapes_the_background_task(self, _apply) -> None:
        """Ingestion is the one thing this process must always keep doing."""
        from webhook import handle_reply

        handle_reply(TextReply(sender_id="s", text="1", payload=None,
                               message_id=None))


if __name__ == "__main__":
    unittest.main()
