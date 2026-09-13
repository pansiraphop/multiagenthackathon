"""Webhook routing — the path a real Instagram DM takes.

Two different things arrive as text on one endpoint, and sending either to the
wrong handler fails quietly: an answer to stage 5b's question treated as chat
never closes the loop, and chat treated as an answer gets swallowed. Neither
raises. So the routing is worth pinning down before pointing ngrok at it.

Nothing here calls Meta or the model.

    python -m tests.test_webhook_routing
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import webhook
from stages.followup import OPEN_STATUS


def text_payload(text: str, sender: str = "17841400000000000") -> dict:
    """The shape Meta actually posts for a plain DM."""
    return {
        "object": "instagram",
        "entry": [{
            "id": "acct",
            "time": 1789000000,
            "messaging": [{
                "sender": {"id": sender},
                "recipient": {"id": "page"},
                "timestamp": 1789000000,
                "message": {"mid": "mid.abc", "text": text},
            }],
        }],
    }


class OpenQuestionCheck(unittest.TestCase):
    """The constant is 'sent', not 'open'. Hardcoding it wrong routes silently."""

    def test_uses_the_real_status_value(self) -> None:
        captured = {}

        def fake_select(table, _cols="*", **eq):
            captured.update(eq)
            return []

        with patch("lib.db.select", side_effect=fake_select):
            webhook._has_open_question("s1")
        self.assertEqual(captured.get("status"), OPEN_STATUS)

    def test_matches_a_question_aimed_at_this_sender(self) -> None:
        with patch("lib.db.select", return_value=[{"recipient_id": "s1"}]):
            self.assertTrue(webhook._has_open_question("s1"))

    def test_matches_a_question_with_no_recipient(self) -> None:
        """Seeded questions predate sender_id and still need answering."""
        with patch("lib.db.select", return_value=[{"recipient_id": None}]):
            self.assertTrue(webhook._has_open_question("s1"))

    def test_ignores_a_question_aimed_at_someone_else(self) -> None:
        with patch("lib.db.select", return_value=[{"recipient_id": "other"}]):
            self.assertFalse(webhook._has_open_question("s1"))

    def test_a_database_failure_does_not_decide_by_crashing(self) -> None:
        with patch("lib.db.select", side_effect=RuntimeError("offline")):
            self.assertFalse(webhook._has_open_question("s1"))


class Routing(unittest.TestCase):
    def test_chat_goes_to_the_concierge(self) -> None:
        with patch("webhook._has_open_question", return_value=False), \
             patch("stages.concierge.handle_dm") as concierge, \
             patch("stages.followup.apply_reply") as followup:
            webhook.handle_reply(
                webhook.TextReply(sender_id="s1", text="what's for dinner",
                                  payload=None, message_id="m1"))
        concierge.assert_called_once_with("s1", "what's for dinner")
        followup.assert_not_called()

    def test_an_answer_goes_to_the_follow_up_loop(self) -> None:
        with patch("webhook._has_open_question", return_value=True), \
             patch("stages.concierge.handle_dm") as concierge, \
             patch("stages.followup.apply_reply") as followup:
            webhook.handle_reply(
                webhook.TextReply(sender_id="s1", text="option 2",
                                  payload=None, message_id="m1"))
        followup.assert_called_once()
        concierge.assert_not_called()

    def test_a_broken_follow_up_still_gets_a_reply(self) -> None:
        """Never leave a DM unanswered because one handler threw."""
        with patch("webhook._has_open_question", return_value=True), \
             patch("stages.followup.apply_reply", side_effect=RuntimeError("boom")), \
             patch("stages.concierge.handle_dm") as concierge:
            webhook.handle_reply(
                webhook.TextReply(sender_id="s1", text="hi",
                                  payload=None, message_id="m1"))
        concierge.assert_called_once()

    def test_a_broken_concierge_does_not_kill_the_worker(self) -> None:
        with patch("webhook._has_open_question", return_value=False), \
             patch("stages.concierge.handle_dm", side_effect=RuntimeError("api")):
            webhook.handle_reply(
                webhook.TextReply(sender_id="s1", text="hi",
                                  payload=None, message_id="m1"))


class EndToEnd(unittest.TestCase):
    """Through the actual HTTP endpoint, with the real payload shape."""

    def setUp(self) -> None:
        self.client = TestClient(webhook.app)

    def test_a_dm_is_acknowledged_and_queued(self) -> None:
        with patch("webhook._has_open_question", return_value=False), \
             patch("stages.concierge.handle_dm") as concierge:
            response = self.client.post("/webhook", json=text_payload("plan my week"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["replies_queued"], 1)
        concierge.assert_called_once()
        self.assertEqual(concierge.call_args[0][1], "plan my week")

    def test_the_sender_id_survives_the_trip(self) -> None:
        """The reply has to go back to whoever sent it."""
        with patch("webhook._has_open_question", return_value=False), \
             patch("stages.concierge.handle_dm") as concierge:
            self.client.post("/webhook", json=text_payload("hi", sender="12345"))
        self.assertEqual(concierge.call_args[0][0], "12345")

    def test_a_reel_is_not_treated_as_chat(self) -> None:
        payload = {
            "object": "instagram",
            "entry": [{"messaging": [{
                "sender": {"id": "s1"},
                "message": {"attachments": [{
                    "type": "ig_reel",
                    "payload": {"reel_video_id": "1", "title": "Katsu curry",
                                "url": "https://instagram.com/reel/x/"},
                }]},
            }]}],
        }
        with patch("webhook.ingest_reel") as ingest, \
             patch("stages.concierge.handle_dm") as concierge:
            response = self.client.post("/webhook", json=payload)

        self.assertEqual(response.json()["reels_queued"], 1)
        ingest.assert_called_once()
        concierge.assert_not_called()

    def test_an_unrecognised_payload_is_acknowledged_not_rejected(self) -> None:
        """Meta retries on non-200, so never fail a delivery we can't parse."""
        response = self.client.post("/webhook", json={"object": "instagram",
                                                      "entry": []})
        self.assertEqual(response.status_code, 200)

    def test_verification_challenge(self) -> None:
        with patch.object(webhook.config, "META_VERIFY_TOKEN", "tok"):
            response = self.client.get("/webhook", params={
                "hub.mode": "subscribe", "hub.verify_token": "tok",
                "hub.challenge": "42"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "42")

    def test_verification_rejects_a_wrong_token(self) -> None:
        with patch.object(webhook.config, "META_VERIFY_TOKEN", "tok"):
            response = self.client.get("/webhook", params={
                "hub.mode": "subscribe", "hub.verify_token": "wrong",
                "hub.challenge": "42"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
