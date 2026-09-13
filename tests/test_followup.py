"""Stage 5b tests — the loop back to the user.

No network and no database: every DM send and every row read is patched. The
sharp edges here are (a) never guessing what an ambiguous reply meant, because a
wrong guess silently moves someone's real evening, and (b) the round bound,
because a feedback loop that can't terminate is worse than no loop.

    python -m tests.test_followup
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import config
from lib.instagram import TextReply, match_option, parse_text_replies
from stages import followup
from stages.availability import _at

MONDAY = date(2026, 9, 14)
WEEK = MONDAY.isoformat()
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=config.TIMEZONE)


def recipe(rid="r1", title="Test Dish", active=30, lead=0, names=("spinach",)):
    return {
        "id": rid,
        "title": title,
        "cuisine": "italian",
        "est_time_minutes": active,
        "total_time_minutes": active,
        "advance_prep_minutes": lead,
        "ingredients": [{"name": n} for n in names],
    }


def slot(sid="s1", day_offset=0, start="18:00", minutes=120):
    begin = _at(MONDAY + timedelta(days=day_offset), start)
    return {
        "id": sid,
        "slot_start": begin.isoformat(),
        "slot_end": (begin + timedelta(minutes=minutes)).isoformat(),
        "duration_minutes": minutes,
    }


def meal(mid="m1", recipe_id="r1", slot_id="s1", start="2026-09-14T18:00:00-07:00"):
    return {
        "id": mid,
        "recipe_id": recipe_id,
        "cook_slot_id": slot_id,
        "week_start_date": WEEK,
        "planned_start_time": start,
        "planned_end_time": start,
    }


def shopping(name, status="pending", unit="unit"):
    return {"ingredient_name": name, "unit": unit, "quantity_needed": 1,
            "resolution_status": status, "week_start_date": WEEK}


def followup_row(**kw):
    row = {
        "id": "f1",
        "week_start_date": WEEK,
        "kind": followup.ISSUE_MISSING,
        "recipient_id": "sender-1",
        "status": "sent",
        "round": 1,
        "created_at": "2026-09-13T12:00:00+00:00",
        "options": [],
    }
    row.update(kw)
    return row


def conflict(**kw):
    row = {
        "slot_id": "s1",
        "meal_id": "m1",
        "title": "Palak Paneer",
        "planned_start": datetime(2026, 9, 14, 18, 50, tzinfo=config.TIMEZONE),
        "delivery_end": datetime(2026, 9, 14, 18, 0, tzinfo=config.TIMEZONE),
        "short_by_minutes": 110,
    }
    row.update(kw)
    return row


class Detection(unittest.TestCase):
    """What counts as needing a human, and which issue is asked about first."""

    def issues(self, *, conflict_row=None, rows=(), meals=(), recipes=None):
        recipes = recipes or {}

        def select(table, *_a, **_kw):
            return {"shopping_list": list(rows), "meal_plan": list(meals)}.get(table, [])

        with (
            patch.object(followup.plan, "delivery_conflict", return_value=conflict_row),
            patch.object(followup.db, "select", side_effect=select),
            patch.object(followup.db, "get_recipe_with_ingredients",
                         side_effect=lambda rid: recipes.get(rid)),
        ):
            return followup.open_issues(MONDAY)

    def test_feasible_week_asks_nothing(self):
        self.assertEqual(self.issues(rows=[shopping("tomato", "added_to_cart")]), [])

    def test_late_delivery_is_an_issue(self):
        issues = self.issues(conflict_row=conflict())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], followup.ISSUE_DELIVERY)
        self.assertIn("110 minutes too late", issues[0]["detail"])

    def test_unresolved_items_name_the_meal_that_needs_them(self):
        issues = self.issues(
            rows=[shopping("turmeric", "failed"), shopping("onion", "added_to_cart")],
            meals=[meal()],
            recipes={"r1": recipe(names=("turmeric", "onion"), title="Palak Paneer")},
        )
        self.assertEqual(issues[0]["kind"], followup.ISSUE_MISSING)
        self.assertIn("turmeric", issues[0]["detail"])
        self.assertIn("Palak Paneer", issues[0]["detail"])
        self.assertEqual(issues[0]["avoid"], {"turmeric"})

    def test_fallback_link_counts_as_unresolved(self):
        issues = self.issues(
            rows=[shopping("turmeric", "fallback_link")],
            meals=[meal()],
            recipes={"r1": recipe(names=("turmeric",))},
        )
        self.assertEqual(len(issues), 1)

    def test_unresolved_item_no_meal_needs_is_not_asked_about(self):
        """A stale row for a meal that got re-planned away is not a question."""
        issues = self.issues(
            rows=[shopping("turmeric", "failed")],
            meals=[meal()],
            recipes={"r1": recipe(names=("onion",))},
        )
        self.assertEqual(issues, [])

    def test_delivery_conflict_is_asked_about_first(self):
        issues = self.issues(
            conflict_row=conflict(),
            rows=[shopping("turmeric", "failed")],
            meals=[meal()],
            recipes={"r1": recipe(names=("turmeric",))},
        )
        self.assertEqual([i["kind"] for i in issues],
                         [followup.ISSUE_DELIVERY, followup.ISSUE_MISSING])

    def test_worst_affected_meal_is_chosen(self):
        issues = self.issues(
            rows=[shopping("turmeric", "failed"), shopping("ghee", "failed")],
            meals=[meal("m1", "r1"), meal("m2", "r2")],
            recipes={
                "r1": recipe("r1", title="One Missing", names=("turmeric",)),
                "r2": recipe("r2", title="Two Missing", names=("turmeric", "ghee")),
            },
        )
        self.assertEqual(issues[0]["meal_id"], "m2")


class SwapCandidates(unittest.TestCase):
    """The alternatives offered are the user's own previously-sent reels."""

    def candidates(self, recipes, *, planned=(), target=None, avoid=frozenset()):
        def select(table, *_a, **_kw):
            if table == "meal_plan":
                return [{"recipe_id": r} for r in planned]
            return []

        with (
            patch.object(followup.db, "select", side_effect=select),
            patch.object(followup.db, "successful_recipes", return_value=recipes),
            patch.object(followup.plan, "usable_pantry", return_value={}),
        ):
            return followup.swap_candidates(MONDAY, slot=target, avoid=set(avoid),
                                            now=NOW)

    def test_already_planned_reels_are_not_offered(self):
        got = self.candidates([recipe("r1"), recipe("r2")], planned=["r1"])
        self.assertEqual([r["id"] for r in got], ["r2"])

    def test_a_reel_too_long_for_the_window_is_not_offered(self):
        got = self.candidates([recipe("long", active=150), recipe("short", active=30)],
                              target=slot(minutes=60))
        self.assertEqual([r["id"] for r in got], ["short"])

    def test_a_marinade_that_cannot_start_in_time_is_not_offered(self):
        """NOW is Sunday noon; a 12-hour marinade can't make Monday 18:00."""
        got = self.candidates([recipe("marinade", lead=48 * 60), recipe("quick")],
                              target=slot(day_offset=0))
        self.assertEqual([r["id"] for r in got], ["quick"])

    def test_reels_needing_the_missing_items_sort_last(self):
        got = self.candidates(
            [recipe("needs-it", names=("turmeric", "rice")),
             recipe("clean", names=("pasta", "oil"))],
            avoid={"turmeric"},
        )
        self.assertEqual([r["id"] for r in got], ["clean", "needs-it"])

    def test_offer_is_capped(self):
        got = self.candidates([recipe(f"r{n}") for n in range(6)])
        self.assertEqual(len(got), config.FOLLOWUP_SWAP_OPTIONS)


class Options(unittest.TestCase):
    def test_delivery_question_offers_reschedule_swap_and_keep(self):
        issue = {"kind": followup.ISSUE_DELIVERY, "title": "Palak Paneer",
                 "slot_id": "s1", "meal_id": "m1", "detail": "too late"}
        options = followup.build_options(issue, [recipe("r9", title="Katsu Curry")])

        self.assertEqual([o["action"] for o in options], ["later", "swap", "keep"])
        self.assertEqual([o["key"] for o in options], ["1", "2", "3"])
        self.assertEqual(options[1]["recipe_id"], "r9")
        self.assertIn("Katsu Curry", options[1]["label"])

    def test_missing_items_question_leads_with_cooking_anyway(self):
        issue = {"kind": followup.ISSUE_MISSING, "title": "Palak Paneer",
                 "slot_id": "s1", "meal_id": "m1", "detail": "no turmeric"}
        options = followup.build_options(issue, [])
        self.assertEqual([o["action"] for o in options], ["keep", "later"])

    def test_question_is_numbered_and_readable(self):
        issue = {"kind": followup.ISSUE_DELIVERY, "title": "Ragu",
                 "slot_id": "s1", "meal_id": "m1",
                 "detail": "the groceries land two hours late"}
        options = followup.build_options(issue, [])
        text = followup.compose(issue, options, final_round=False)

        self.assertIn("the groceries land two hours late", text)
        self.assertIn("1. Move Ragu later this week", text)
        self.assertNotIn("last check-in", text)

    def test_final_round_says_it_is_the_last_ask(self):
        issue = {"kind": followup.ISSUE_MISSING, "title": "Ragu",
                 "slot_id": None, "meal_id": "m1", "detail": "x"}
        text = followup.compose(issue, followup.build_options(issue, []),
                                final_round=True)
        self.assertIn("last check-in", text)


class ReplyMatching(unittest.TestCase):
    """People answer '2', 'two', 'reschedule', and 'yes please'."""

    OPTIONS = [
        {"key": "1", "action": "later", "label": "Move it later this week"},
        {"key": "2", "action": "swap", "label": "Cook Katsu Curry instead"},
        {"key": "3", "action": "keep", "label": "Keep it as planned"},
    ]

    def choose(self, text, payload=None):
        reply = TextReply(sender_id="s", text=text, payload=payload, message_id=None)
        return match_option(reply, self.OPTIONS)

    def test_quick_reply_payload_wins(self):
        self.assertEqual(self.choose("Cook Katsu Curry instead",
                                     payload="INSTACOOK_2")["key"], "2")

    def test_bare_number(self):
        self.assertEqual(self.choose("2")["key"], "2")

    def test_number_with_padding(self):
        self.assertEqual(self.choose("#3 please")["key"], "3")

    def test_number_word(self):
        self.assertEqual(self.choose("two")["key"], "2")

    def test_exact_label(self):
        self.assertEqual(self.choose("keep it as planned")["key"], "3")

    def test_keyword_when_it_maps_to_one_option(self):
        self.assertEqual(self.choose("can you reschedule it")["key"], "1")

    def test_number_inside_a_sentence(self):
        self.assertEqual(self.choose("yes, 1")["key"], "1")

    def test_out_of_range_number_is_not_an_answer(self):
        self.assertIsNone(self.choose("9"))

    def test_two_numbers_is_not_an_answer(self):
        self.assertIsNone(self.choose("1 or 2?"))

    def test_ambiguous_reply_is_not_guessed(self):
        """'move it or swap it, whatever' names two actions. Guessing is worse."""
        self.assertIsNone(self.choose("move it or swap it, whatever's easier"))

    def test_unrelated_message_is_not_an_answer(self):
        self.assertIsNone(self.choose("hey what's for dinner"))

    def test_no_options_means_no_match(self):
        reply = TextReply(sender_id="s", text="1", payload=None, message_id=None)
        self.assertIsNone(match_option(reply, []))


class WebhookParsing(unittest.TestCase):
    def payload(self, message, sender="sender-1"):
        return {"object": "instagram",
                "entry": [{"messaging": [{"sender": {"id": sender},
                                          "message": message}]}]}

    def test_text_reply_is_parsed(self):
        replies = parse_text_replies(self.payload({"mid": "m1", "text": " 2 "}))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].text, "2")
        self.assertEqual(replies[0].sender_id, "sender-1")

    def test_quick_reply_payload_is_carried(self):
        replies = parse_text_replies(self.payload(
            {"text": "Move it", "quick_reply": {"payload": "INSTACOOK_1"}}))
        self.assertEqual(replies[0].payload, "INSTACOOK_1")

    def test_our_own_echo_is_ignored(self):
        """Without this the agent answers its own question."""
        self.assertEqual(
            parse_text_replies(self.payload({"text": "1. Move it", "is_echo": True})),
            [])

    def test_a_reel_is_not_a_reply(self):
        """Reels belong to stage 1; treating one as an answer would act on nothing."""
        self.assertEqual(
            parse_text_replies(self.payload(
                {"text": "check this out",
                 "attachments": [{"type": "ig_reel", "payload": {"url": "u"}}]})),
            [])

    def test_empty_text_is_ignored(self):
        self.assertEqual(parse_text_replies(self.payload({"text": "   "})), [])

    def test_other_objects_are_ignored(self):
        self.assertEqual(parse_text_replies({"object": "page", "entry": []}), [])


class Asking(unittest.TestCase):
    def ask(self, *, delivered=True, open_rows=(), dry_run=False):
        from lib.instagram import Delivery

        def select(table, *_a, **_kw):
            if table == "followups":
                return list(open_rows)
            return []

        sent = MagicMock(return_value=Delivery(delivered, "instagram_dm"
                                               if delivered else "console", "mid-1"))
        with (
            patch.object(followup.db, "select", side_effect=select),
            patch.object(followup.db, "successful_recipes", return_value=[]),
            patch.object(followup.plan, "usable_pantry", return_value={}),
            patch.object(followup.db, "latest_sender_id", return_value="sender-1"),
            patch.object(followup.db, "insert",
                         side_effect=lambda _t, row: [dict(row, id="f-new")]) as insert,
            patch.object(followup.db, "update") as update,
            patch.object(followup.instagram, "send_dm", sent),
            patch.object(followup, "log_eval"),
        ):
            issue = {"kind": followup.ISSUE_DELIVERY, "title": "Ragu",
                     "slot_id": None, "meal_id": "m1", "detail": "too late",
                     "avoid": set()}
            row = followup.ask(MONDAY, issue, dry_run=dry_run)
        return row, insert, update, sent

    def test_sent_question_is_recorded_as_open(self):
        row, _insert, _update, sent = self.ask()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["channel"], "instagram_dm")
        self.assertEqual(row["provider_message_id"], "mid-1")
        sent.assert_called_once()

    def test_undelivered_question_is_recorded_as_unreachable(self):
        """Nobody can answer a print statement, so it must not read as open."""
        row, _insert, _update, _sent = self.ask(delivered=False)
        self.assertEqual(row["status"], "unreachable")

    def test_asking_again_supersedes_the_previous_question(self):
        _row, _insert, update, _sent = self.ask(open_rows=[followup_row(id="old")])
        update.assert_called_once_with("followups", "old", {"status": "superseded"})

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        row, insert, _update, sent = self.ask(dry_run=True)
        self.assertIsNone(row)
        sent.assert_not_called()
        insert.assert_not_called()


class ApplyingAnAnswer(unittest.TestCase):
    def apply(self, text, options, *, open_rows=None, sender="sender-1",
              meals=None, target_recipe=None):
        rows = open_rows if open_rows is not None else [followup_row(options=options)]

        def select(table, _cols="*", **kw):
            if table == "followups":
                return list(rows)
            if table == "meal_plan":
                return list(meals or [])
            return []

        harness = {
            "select": select,
            "update": MagicMock(),
            "plan_run": MagicMock(),
            "shopping_run": MagicMock(),
            "instacart_run": MagicMock(),
            "calendar_run": MagicMock(),
            "send": MagicMock(),
        }
        with (
            patch.object(followup.db, "select", side_effect=select),
            patch.object(followup.db, "update", harness["update"]),
            patch.object(followup.db, "get_recipe_with_ingredients",
                         return_value=target_recipe),
            patch.object(followup.plan, "run", harness["plan_run"]),
            patch.object(followup.shopping_list, "run", harness["shopping_run"]),
            patch.object(followup.instacart, "run", harness["instacart_run"]),
            patch.object(followup.calendar_sync, "run", harness["calendar_run"]),
            patch.object(followup.instagram, "send_dm", harness["send"]),
            patch.object(followup, "run", MagicMock(return_value=None)) as recheck,
            patch.object(followup, "log_eval"),
        ):
            result = followup.apply_reply(
                TextReply(sender_id=sender, text=text, payload=None, message_id=None),
                week_start=MONDAY,
            )
        harness["recheck"] = recheck
        harness["result"] = result
        return harness

    def test_reschedule_replans_without_that_window(self):
        options = [{"key": "1", "action": "later", "label": "Move it",
                    "slot_id": "s1"}]
        h = self.apply("1", options)

        h["plan_run"].assert_called_once()
        self.assertEqual(h["plan_run"].call_args.kwargs["exclude_slots"], ("s1",))
        h["shopping_run"].assert_called_once()
        h["instacart_run"].assert_called_once()
        self.assertFalse(h["instacart_run"].call_args.kwargs.get("place_order_flag"))
        h["calendar_run"].assert_called_once()

    def test_swap_keeps_the_window_and_changes_the_dish(self):
        options = [{"key": "1", "action": "swap", "label": "Cook Katsu instead",
                    "recipe_id": "r9", "meal_id": "m1"}]
        h = self.apply("1", options, meals=[meal()],
                       target_recipe=recipe("r9", title="Katsu", active=45))

        h["plan_run"].assert_not_called()   # the user chose this night on purpose
        meal_update = [c for c in h["update"].call_args_list
                       if c.args[0] == "meal_plan"][0]
        self.assertEqual(meal_update.args[2]["recipe_id"], "r9")
        self.assertEqual(meal_update.args[2]["planned_end_time"],
                         "2026-09-14T18:45:00-07:00")
        h["shopping_run"].assert_called_once()
        h["instacart_run"].assert_called_once()

    def test_keep_changes_no_plan(self):
        options = [{"key": "1", "action": "keep", "label": "Cook it anyway"}]
        h = self.apply("1", options)

        h["plan_run"].assert_not_called()
        h["shopping_run"].assert_not_called()
        h["instacart_run"].assert_not_called()

    def test_answer_is_recorded_against_the_question(self):
        options = [{"key": "1", "action": "keep", "label": "Cook it anyway"}]
        h = self.apply("yes, 1", options)

        recorded = [c for c in h["update"].call_args_list
                    if c.args[0] == "followups"][0].args[2]
        self.assertEqual(recorded["status"], "answered")
        self.assertEqual(recorded["chosen_key"], "1")
        self.assertEqual(recorded["reply_text"], "yes, 1")
        self.assertTrue(recorded["resolution"])

    def test_unmatched_reply_asks_again_instead_of_acting(self):
        options = [{"key": "1", "action": "later", "label": "Move it",
                    "slot_id": "s1"}]
        h = self.apply("what?", options)

        self.assertIsNone(h["result"])
        h["plan_run"].assert_not_called()
        h["update"].assert_not_called()
        self.assertIn("didn't catch that", h["send"].call_args.args[1])

    def test_a_reply_from_someone_else_is_ignored(self):
        options = [{"key": "1", "action": "later", "label": "Move it"}]
        h = self.apply("1", options, sender="someone-else")

        self.assertIsNone(h["result"])
        h["plan_run"].assert_not_called()
        h["send"].assert_not_called()

    def test_no_open_question_means_nothing_happens(self):
        h = self.apply("1", [], open_rows=[])
        self.assertIsNone(h["result"])
        h["plan_run"].assert_not_called()

    def test_the_newest_question_is_the_one_answered(self):
        old = followup_row(id="old", created_at="2026-09-13T10:00:00+00:00",
                           options=[{"key": "1", "action": "keep", "label": "Old"}])
        new = followup_row(id="new", created_at="2026-09-13T18:00:00+00:00",
                           options=[{"key": "1", "action": "keep", "label": "New"}])
        h = self.apply("1", None, open_rows=[old, new])

        answered = [c for c in h["update"].call_args_list
                    if c.args[0] == "followups"][0]
        self.assertEqual(answered.args[1], "new")

    def test_the_new_plan_gets_the_same_check(self):
        """The loop: applying an answer can create the next conflict."""
        options = [{"key": "1", "action": "later", "label": "Move it",
                    "slot_id": "s1"}]
        h = self.apply("1", options)

        h["recheck"].assert_called_once()
        self.assertEqual(h["recheck"].call_args.kwargs["round_number"], 2)


class RoundBound(unittest.TestCase):
    """The loop has to terminate, and has to say so when it does."""

    def run_stage(self, *, round_number, open_rows=(), issues=None):
        issue = {"kind": followup.ISSUE_DELIVERY, "title": "Ragu",
                 "slot_id": "s1", "meal_id": "m1", "detail": "too late",
                 "avoid": set()}
        with (
            patch.object(followup, "open_issues",
                         return_value=issues if issues is not None else [issue]),
            patch.object(followup.db, "select", return_value=list(open_rows)),
            patch.object(followup, "ask") as ask,
            patch.object(followup, "auto_resolve", return_value="auto") as auto,
            patch.object(followup, "log_eval"),
        ):
            result = followup.run(MONDAY, round_number=round_number)
        return result, ask, auto

    def test_a_feasible_week_asks_nothing(self):
        result, ask, auto = self.run_stage(round_number=1, issues=[])
        self.assertIsNone(result)
        ask.assert_not_called()
        auto.assert_not_called()

    def test_first_round_asks(self):
        _result, ask, auto = self.run_stage(round_number=1)
        ask.assert_called_once()
        auto.assert_not_called()

    def test_past_the_bound_it_stops_asking_and_resolves_itself(self):
        _result, ask, auto = self.run_stage(
            round_number=config.FOLLOWUP_MAX_ROUNDS + 1)
        ask.assert_not_called()
        auto.assert_called_once()

    def test_an_unanswered_question_is_not_asked_twice(self):
        """Re-running the pipeline must not spam the same question."""
        _result, ask, _auto = self.run_stage(
            round_number=1, open_rows=[followup_row(kind=followup.ISSUE_DELIVERY)])
        ask.assert_not_called()

    def test_an_open_question_about_something_else_does_not_block(self):
        _result, ask, _auto = self.run_stage(
            round_number=1, open_rows=[followup_row(kind=followup.ISSUE_MISSING)])
        ask.assert_called_once()


class UnreachableUser(unittest.TestCase):
    def test_no_dm_channel_falls_back_to_deciding_alone(self):
        """The old deterministic behaviour is the floor, not the first move."""
        issue = {"kind": followup.ISSUE_DELIVERY, "title": "Ragu",
                 "slot_id": "s1", "meal_id": "m1", "detail": "too late",
                 "avoid": set()}
        with (
            patch.object(followup, "open_issues", return_value=[issue]),
            patch.object(followup.db, "select", return_value=[]),
            patch.object(followup, "ask",
                         return_value=followup_row(status="unreachable")),
            patch.object(followup.db, "update") as update,
            patch.object(followup, "_rebuild") as rebuild,
            patch.object(followup, "log_eval"),
        ):
            followup.run(MONDAY)

        rebuild.assert_called_once()
        self.assertEqual(rebuild.call_args.kwargs["exclude_slots"], ("s1",))
        self.assertIn("no reply reachable", update.call_args.args[2]["resolution"])


if __name__ == "__main__":
    unittest.main()
