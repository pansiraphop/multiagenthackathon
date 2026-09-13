"""Concierge tests — the DM agent's tools and conversation handling.

The model is mocked throughout: these check that the tools read and write the
right rows, that conversation memory behaves, and that a failing model never
leaves a DM unanswered. Whether the model picks the right tool is a prompt
question, not something a unit test can pin down.

    python -m tests.test_concierge
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import config
from stages import concierge


def run_tool(tool, **kwargs):
    """Call one of the @beta_tool-decorated functions directly."""
    fn = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool
    return fn(**kwargs)


PANTRY = [
    {"id": "p1", "ingredient_name": "spinach", "quantity": 400, "unit": "g",
     "expiry_date": (config.now_local().date() + timedelta(days=2)).isoformat()},
    {"id": "p2", "ingredient_name": "rice", "quantity": 2, "unit": "kg",
     "expiry_date": None},
]


class PantryTools(unittest.TestCase):
    def test_lists_items_with_urgency(self) -> None:
        with patch("lib.db.select", return_value=PANTRY):
            out = run_tool(concierge.get_pantry)
        self.assertIn("spinach", out)
        self.assertIn("2d left", out)
        self.assertIn("staple", out)

    def test_empty_pantry_says_so(self) -> None:
        with patch("lib.db.select", return_value=[]):
            self.assertIn("empty", run_tool(concierge.get_pantry).lower())

    def test_add_normalizes_before_writing(self) -> None:
        """'2 Salmon Fillets' must land as 'salmon fillet' or nothing matches."""
        with patch("lib.db.select", return_value=[]), \
             patch("lib.db.insert") as insert:
            out = run_tool(concierge.add_pantry_items, items=json.dumps(
                [{"name": "2 Salmon Fillets", "quantity": 2, "unit": "unit"}]))
        written = insert.call_args[0][1]
        self.assertEqual(written["ingredient_name"], "salmon fillet")
        self.assertIn("salmon fillet", out)

    def test_add_updates_an_existing_item_rather_than_duplicating(self) -> None:
        with patch("lib.db.select", return_value=[PANTRY[0]]), \
             patch("lib.db.update") as update, patch("lib.db.insert") as insert:
            run_tool(concierge.add_pantry_items,
                     items=json.dumps([{"name": "spinach", "quantity": 900,
                                        "unit": "g"}]))
        update.assert_called_once()
        insert.assert_not_called()

    def test_add_accepts_a_single_object(self) -> None:
        with patch("lib.db.select", return_value=[]), patch("lib.db.insert"):
            out = run_tool(concierge.add_pantry_items,
                           items=json.dumps({"name": "leek"}))
        self.assertIn("leek", out)

    def test_bad_json_is_reported_not_raised(self) -> None:
        out = run_tool(concierge.add_pantry_items, items="not json at all")
        self.assertIn("could not read", out.lower())

    def test_remove_missing_item_is_not_an_error(self) -> None:
        with patch("lib.db.select", return_value=[]):
            self.assertIn("isn't in the pantry",
                          run_tool(concierge.remove_pantry_item, name="saffron"))


class PlanTools(unittest.TestCase):
    MEAL = {
        "id": "m1", "recipe_id": "r1", "week_start_date": "2026-09-14",
        "planned_start_time": datetime(2026, 9, 15, 19, 0,
                                       tzinfo=config.TIMEZONE).isoformat(),
        "score_reason": "Uses the spinach expiring Wednesday.",
    }
    RECIPE = {
        "id": "r1", "title": "Palak Paneer", "cuisine": "indian",
        "est_time_minutes": 35, "total_time_minutes": 35,
        "advance_prep_minutes": 0, "servings": 4,
        "ingredients": [{"name": "spinach"}, {"name": "paneer"}],
    }

    def test_empty_week_says_so(self) -> None:
        with patch("lib.db.select", return_value=[]):
            self.assertIn("nothing is planned",
                          run_tool(concierge.get_week_plan).lower())

    def test_week_plan_includes_day_time_and_reason(self) -> None:
        with patch("lib.db.select", return_value=[self.MEAL]), \
             patch("lib.db.successful_recipes", return_value=[self.RECIPE]):
            out = run_tool(concierge.get_week_plan)
        self.assertIn("Tuesday", out)
        self.assertIn("Palak Paneer", out)
        self.assertIn("spinach expiring", out)

    def test_planning_refuses_to_silently_replace(self) -> None:
        """Overwriting a week someone already arranged needs saying yes to."""
        with patch("lib.db.select", return_value=[self.MEAL]), \
             patch("stages.availability.run") as avail, \
             patch("stages.plan.run") as planner:
            out = run_tool(concierge.plan_week, replace_existing=False)
        avail.assert_not_called()
        planner.assert_not_called()
        self.assertIn("replace_existing", out)

    def test_planning_proceeds_when_told_to(self) -> None:
        with patch("lib.db.select", return_value=[]), \
             patch("lib.db.successful_recipes", return_value=[self.RECIPE]), \
             patch("stages.availability.run", return_value=[{"id": "s1"}]), \
             patch("stages.plan.run", return_value=[self.MEAL]) as planner:
            out = run_tool(concierge.plan_week, replace_existing=True)
        planner.assert_called_once()
        self.assertIn("Planned 1 meals", out)

    def test_no_cook_windows_is_reported(self) -> None:
        with patch("lib.db.select", return_value=[]), \
             patch("stages.availability.run", return_value=[]):
            self.assertIn("calendar", run_tool(concierge.plan_week).lower())

    def test_whats_for_dinner_by_weekday(self) -> None:
        with patch("lib.db.select", return_value=[self.MEAL]), \
             patch("lib.db.successful_recipes", return_value=[self.RECIPE]):
            out = run_tool(concierge.whats_for_dinner, day="tuesday")
        self.assertIn("Palak Paneer", out)
        self.assertIn("/cook/m1", out, "the recipe link is the useful part")

    def test_whats_for_dinner_on_a_free_day(self) -> None:
        with patch("lib.db.select", return_value=[self.MEAL]), \
             patch("lib.db.successful_recipes", return_value=[self.RECIPE]):
            self.assertIn("Nothing planned",
                          run_tool(concierge.whats_for_dinner, day="sunday"))


class Conversation(unittest.TestCase):
    def test_history_is_oldest_first(self) -> None:
        """The model reads it as a transcript; reversed order changes meaning."""
        rows = [
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "first"},
        ]                                   # the query returns newest first
        chain = MagicMock()
        chain.execute.return_value = MagicMock(data=rows)
        for method in ("select", "eq", "order", "limit"):
            getattr(chain, method).return_value = chain
        with patch("lib.db.client") as client:
            client.return_value.table.return_value = chain
            out = concierge.history("s1")
        self.assertEqual([t["content"] for t in out], ["first", "second"])

    def test_turns_are_remembered(self) -> None:
        final = MagicMock()
        final.content = [MagicMock(type="text", text="Tuesday, 7pm.")]
        runner = MagicMock()
        runner.until_done.return_value = final

        with patch("stages.concierge.history", return_value=[]), \
             patch("stages.concierge.remember") as remember, \
             patch("stages.concierge.log_eval"), \
             patch("stages.concierge.llm_client") as llm:
            llm.return_value.beta.messages.tool_runner.return_value = runner
            reply = concierge.respond("s1", "what's for dinner")

        self.assertEqual(reply, "Tuesday, 7pm.")
        self.assertEqual(
            [c.args[1] for c in remember.call_args_list], ["user", "assistant"])

    def test_a_model_failure_still_answers(self) -> None:
        """Silence reads as broken. Say something true instead."""
        with patch("stages.concierge.history", return_value=[]), \
             patch("stages.concierge.remember"), \
             patch("stages.concierge.log_eval") as log, \
             patch("stages.concierge.llm_client", side_effect=RuntimeError("api down")):
            reply = concierge.respond("s1", "hello")

        self.assertTrue(reply.strip())
        self.assertFalse(log.call_args[0][2], "the failure must be logged as one")

    def test_an_empty_model_reply_is_not_sent_blank(self) -> None:
        final = MagicMock()
        final.content = []
        runner = MagicMock()
        runner.until_done.return_value = final
        with patch("stages.concierge.history", return_value=[]), \
             patch("stages.concierge.remember"), \
             patch("stages.concierge.log_eval"), \
             patch("stages.concierge.llm_client") as llm:
            llm.return_value.beta.messages.tool_runner.return_value = runner
            self.assertTrue(concierge.respond("s1", "hi").strip())

    def test_dm_send_failure_does_not_lose_the_reply(self) -> None:
        with patch("stages.concierge.respond", return_value="Planned."), \
             patch("lib.instagram.configured", return_value=True), \
             patch("lib.instagram.send_dm", side_effect=RuntimeError("429")):
            self.assertEqual(concierge.handle_dm("s1", "plan it"), "Planned.")


class ToolSurface(unittest.TestCase):
    def test_every_tool_is_registered(self) -> None:
        self.assertEqual(len(concierge.TOOLS), 9)

    def test_tools_carry_descriptions_for_the_model(self) -> None:
        """The docstring IS the tool description the model chooses from."""
        for tool in concierge.TOOLS:
            self.assertTrue((tool.description or "").strip(),
                            f"{tool.name} has no description")

    def test_tool_names_are_unique(self) -> None:
        names = [tool.name for tool in concierge.TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_write_tools_declare_their_arguments(self) -> None:
        by_name = {t.name: t for t in concierge.TOOLS}
        self.assertIn("items", by_name["add_pantry_items"].input_schema["properties"])
        self.assertIn("replace_existing", by_name["plan_week"].input_schema["properties"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
