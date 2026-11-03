"""Rounds (leader, then every other agent once in random order), the message-gap world clock, OpenAI Flex,
and provider waits that pause a run instead of ending it. Fake providers only, no paid calls."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from swarm_bench.common import ROOT
from swarm_bench.experiment_design import expand, validate_design
from swarm_bench.lab_engine import LabRun, _is_rate_limit
from swarm_bench.lab_server import LabManager
from swarm_bench.providers import FLEX_TIMEOUT_SECONDS, ProviderError, ProviderRegistry, completion

USAGE = {"input_tokens": 4, "output_tokens": 2}


def call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def rounds_design(after_rounds=2, sellers=3, **rules):
    return {
        "name": "Rounds", "task": "Agree on a price, {handle}.",
        "world": {"use_handles": True, "channel": "#deal channel", "start_time": "2026-10-12T09:30",
                  "message_gap_minutes": [1, 6]},
        "roles": [
            {"id": "buyer", "name": "Buyer", "handle": "buyer", "identity": "You are {handle}.",
             "tools": ["read_board", "post_note", "sign"], "leader": True},
            {"id": "seller", "name": "Seller", "count": sellers, "handle": "seller-{n}", "identity": "You are {handle}.",
             "tools": ["read_board", "post_note"], "start_after": "buyer"},
        ],
        "custom_tools": [{"name": "sign", "description": "Sign the deal.",
                          "parameters": [{"name": "price", "type": "integer"}],
                          "response": "Signed at {price}.", "ends_run": True}],
        "controller_messages": [{"after_rounds": after_rounds, "to_roles": ["buyer"], "content": "Decide now.",
                                 "stop_others": True}],
        "rules": {"board_delivery": "push", "turn_order": "rounds", **rules},
    }


def who(messages):
    return messages[0]["content"].split(".")[0].removeprefix("You are ")


class Recorder:
    """A scripted model: everyone posts once per call, the buyer signs after the controller message.

    It records who called, and fails the test if two calls ever overlap."""
    def __init__(self, slow=None, fail=None):
        self.calls, self.active, self.overlap, self.lock = [], 0, False, threading.Lock()
        self.slow, self.fail = slow or {}, fail or {}

    def __call__(self, profile, key, messages, tools, *args):
        name = who(messages)
        with self.lock:
            self.active += 1
            self.overlap |= self.active > 1
            self.calls.append((name, dict(profile)))
            attempt = sum(c[0] == name for c in self.calls)
        try:
            time.sleep(self.slow.get(name, 0))
            error = self.fail.get((name, attempt))
            if error:
                raise error
            if name == "buyer" and any(m.get("content") == "Decide now." for m in messages):
                return {"role": "assistant", "content": "", "tool_calls": [call("sign", {"price": 100}, f"s{attempt}")]}, USAGE
            return ({"role": "assistant", "content": "", "tool_calls": [call("post_note", {"content": f"{name} #{attempt}"}, f"p{attempt}")]},
                    USAGE)
        finally:
            with self.lock:
                self.active -= 1


class QuickEvent(threading.Event):
    """Provider waits of a minute shrink to a moment, so a test can follow a whole retry."""
    def wait(self, timeout=None):
        return super().wait(None if timeout is None else min(timeout, 0.01))


class RunHelper(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_rounds(self, design, responder, profiles=None, **config):
        models = {a["agent_id"]: "fake" for a in expand(validate_design(design))}
        run = LabRun({"scenario": "designed", "design": design, "mode": "live", "models": models, **config},
                     ROOT / "data", self.root / f"run{len(list(self.root.iterdir()))}")
        run.state["provider_profiles"] = profiles or {a: {"kind": "fake"} for a in run.agents}
        run._provider_keys = {a: "" for a in run.agents}
        run.pause_requested = QuickEvent()
        with patch("swarm_bench.lab_engine.completion", responder):
            worker = threading.Thread(target=run.run_free, daemon=True)
            worker.start()
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the run did not finish")
        return run

    def assert_rounds(self, run, rounds):
        """The channel reads leader, every seller once, leader, ... and each round's order is stored."""
        authors = [n["agent_id"] for n in run.state["notes"]]
        sellers = sorted(a for a in run.agents if a != "agent_01")
        orders = run.state["rounds"]["orders"]
        self.assertEqual(len(orders), rounds)
        expected = []
        for order in orders:
            self.assertEqual(sorted(order), sellers)
            expected += ["agent_01"] + order
        self.assertEqual(authors, expected)


class RoundsTests(RunHelper):
    def test_leader_then_each_other_agent_once_then_the_controller_message_after_two_rounds(self):
        recorder = Recorder()
        run = self.run_rounds(rounds_design(after_rounds=2), recorder)
        self.assertFalse(recorder.overlap, "two agents were called at the same time")
        self.assert_rounds(run, 2)
        self.assertEqual(run.state["rounds"]["completed"], 2)
        message = next(e for e in run.state["events"] if e["kind"] == "controller_message")
        self.assertEqual((message["agent_id"], message["rounds_completed"], message["note_count"]), ("agent_01", 2, 8))
        self.assertEqual((run.state["status"], run.state["finish_reason"]), ("complete", "action_ended"))
        self.assertEqual(recorder.calls[-1][0], "buyer")
        self.assertEqual(run.metrics()["rounds"]["completed"], 2)

    def test_order_comes_from_the_system_random_source_not_the_run_seed(self):
        drawn = []
        def shuffle(items):
            items.reverse()
            drawn.append(list(items))
        with patch("swarm_bench.lab_engine.SYSTEM_RANDOM.shuffle", shuffle):
            run = self.run_rounds(rounds_design(after_rounds=2), Recorder(), seed=42)
        self.assertEqual(run.state["rounds"]["orders"], drawn)
        self.assertEqual(drawn[0], ["agent_04", "agent_03", "agent_02"])
        # Two runs with the same seed still draw their own orders.
        orders = set()
        for _ in range(8):
            other = self.run_rounds(rounds_design(after_rounds=1, sellers=4), Recorder(), seed=42)
            orders.add(tuple(other.state["rounds"]["orders"][0]))
        self.assertGreater(len(orders), 1)

    def test_a_slow_leader_keeps_the_others_waiting(self):
        recorder = Recorder(slow={"buyer": 0.3})
        run = self.run_rounds(rounds_design(after_rounds=1), recorder)
        self.assertFalse(recorder.overlap)
        self.assert_rounds(run, 1)

    def test_a_rate_limit_or_timeout_keeps_the_turn_and_uses_no_call(self):
        limited = ProviderError("HTTP 429", retryable=True, diagnostic={"http_status": 429})
        timed_out = ProviderError("timeout", retryable=True, diagnostic={"timeout_seconds": 380})
        # Five provider waits in a row for one seller: more than transient_retries ever allowed.
        fail = {("seller-1", n): (limited if n % 2 else timed_out) for n in range(1, 6)}
        recorder = Recorder(fail=fail)
        run = self.run_rounds(rounds_design(after_rounds=1), recorder, call_limit=3)
        self.assertFalse(recorder.overlap)
        self.assert_rounds(run, 1)
        # The waiting seller made six requests: five waits that were given back, one that counted.
        self.assertEqual(sum(c[0] == "seller-1" for c in recorder.calls), 6)
        self.assertEqual(run.state["usage"]["agent_02"]["calls"], 1)
        reasons = [e["reason"] for e in run.state["events"] if e["kind"] == "retry_scheduled"]
        self.assertEqual(reasons, ["rate_limit", "timeout", "rate_limit", "timeout", "rate_limit"])
        self.assertEqual((run.state["status"], run.state["finish_reason"]), ("complete", "action_ended"))

    def test_the_third_timeout_in_a_row_stops_the_agent(self):
        timed_out = ProviderError("timeout", retryable=True, diagnostic={"timeout_seconds": 380})
        recorder = Recorder(fail={("seller-1", n): timed_out for n in range(1, 4)})
        run = self.run_rounds(rounds_design(after_rounds=1), recorder)
        self.assertEqual(sum(c[0] == "seller-1" for c in recorder.calls), 3)
        error = next(e for e in run.state["events"] if e["kind"] == "error")
        self.assertEqual(error["agent_id"], "agent_02")
        self.assertIn("3 times in a row", error["message"])
        self.assertNotIn("agent_02", [n["agent_id"] for n in run.state["notes"]])
        self.assertEqual(run.state["finish_reason"], "action_ended")  # The others went on without that seller.
        # Two timeouts, then an answer: the count starts again.
        recorder = Recorder(fail={("seller-1", 1): timed_out, ("seller-1", 2): timed_out,
                                  ("seller-1", 4): timed_out, ("seller-1", 5): timed_out})
        run = self.run_rounds(rounds_design(after_rounds=2), recorder)
        self.assertFalse([e for e in run.state["events"] if e["kind"] == "error"])
        self.assert_rounds(run, 2)

    def test_the_leader_timing_out_three_times_ends_the_run(self):
        timed_out = ProviderError("timeout", retryable=True, diagnostic={"timeout_seconds": 1080})
        recorder = Recorder(fail={("buyer", n): timed_out for n in range(1, 4)})
        run = self.run_rounds(rounds_design(after_rounds=1), recorder)
        self.assertEqual({c[0] for c in recorder.calls}, {"buyer"})
        self.assertEqual(run.state["status"], "error")

    def test_no_credit_is_retried_for_a_while_then_stops(self):
        refused = ProviderError("Provider refused the call: no credit left (credit_balance_exhausted).",
                                diagnostic={"http_status": 429, "action_required": True})
        # Credit added in time: the run goes on.
        recorder = Recorder(fail={("seller-2", n): refused for n in range(1, 4)})
        run = self.run_rounds(rounds_design(after_rounds=1), recorder)
        self.assert_rounds(run, 1)
        self.assertEqual([e["reason"] for e in run.state["events"] if e["kind"] == "retry_scheduled"],
                         ["action_required"] * 3)
        self.assertEqual(run.state["usage"]["agent_03"]["calls"], 1)
        # Still refused after the window: the agent stops with the message.
        with patch("swarm_bench.lab_engine.ACTION_REQUIRED_WAIT_SECONDS", 0.05):
            recorder = Recorder(fail={("seller-2", n): refused for n in range(1, 1000)})
            def slow(*args):
                time.sleep(0.01)
                return recorder(*args)
            run = self.run_rounds(rounds_design(after_rounds=1), slow)
        error = next(e for e in run.state["events"] if e["kind"] == "error")
        self.assertEqual(error["agent_id"], "agent_03")
        self.assertIn("This agent stops", error["message"])

    def test_an_agent_that_fails_for_good_passes_its_turn_on(self):
        broken = ProviderError("Provider returned HTTP 400.", diagnostic={"http_status": 400})
        recorder = Recorder(fail={("seller-2", 1): broken})
        run = self.run_rounds(rounds_design(after_rounds=2), recorder)
        # The run's end marks everyone done; the error event stays.
        self.assertEqual([e["agent_id"] for e in run.state["events"] if e["kind"] == "error"], ["agent_03"])
        authors = [n["agent_id"] for n in run.state["notes"]]
        self.assertNotIn("agent_03", authors)
        self.assertEqual(authors.count("agent_01"), 2)
        self.assertEqual(run.state["rounds"]["completed"], 2)

    def test_a_turn_without_a_new_message_gets_a_continuation(self):
        last_roles = []
        def responder(profile, key, messages, tools, *args):
            name = who(messages)
            turns = sum(m["role"] == "assistant" for m in messages)
            last_roles.append(messages[-1]["role"])
            if name == "buyer" and any(m.get("content") == "Decide now." for m in messages):
                return {"role": "assistant", "content": "", "tool_calls": [call("sign", {"price": 90}, "s")]}, USAGE
            if turns == 1:
                return {"role": "assistant", "content": "Let me think."}, USAGE  # Round 2: a turn without a message.
            return {"role": "assistant", "content": "", "tool_calls": [call("post_note", {"content": name}, f"p{turns}")]}, USAGE
        run = self.run_rounds(rounds_design(after_rounds=3, sellers=2), responder)
        # Round 3 starts with nothing new for the buyer: a continuation gives it a user turn to answer.
        nudged = [e["agent_id"] for e in run.state["events"] if e["kind"] == "continuation_requested"]
        self.assertEqual(nudged, ["agent_01"])
        self.assertNotIn("assistant", last_roles)
        self.assertEqual(run.state["rounds"]["completed"], 3)
        self.assertEqual([n["agent_id"] for n in run.state["notes"]].count("agent_01"), 2)
        self.assertEqual(run.state["finish_reason"], "action_ended")

    def test_channel_times_follow_the_message_gap_not_the_real_clock(self):
        recorder = Recorder(slow={"seller-1": 0.2})
        run = self.run_rounds(rounds_design(after_rounds=2), recorder)
        times = [datetime.strptime(n["world_at"], "%Y-%m-%dT%H:%M") for n in run.state["notes"]]
        self.assertEqual(times[0], datetime(2026, 10, 12, 9, 30))
        for earlier, later in zip(times, times[1:]):
            self.assertTrue(timedelta(minutes=1) <= later - earlier <= timedelta(minutes=6))
        board = run.action("agent_01", "read_board", {})["messages"]
        self.assertEqual([m["time"] for m in board], [t.strftime("%Y-%m-%d %H:%M") for t in times])

    def test_validation(self):
        cases = {
            "exactly one agent": lambda d: d["roles"][0].update(count=2),
            "needs rules.turn_order rounds": lambda d: d["rules"].update(turn_order="free"),
            "idle_policy finish": lambda d: d["rules"].update(idle_policy="continue"),
            "needs a world.start_time": lambda d: d["world"].pop("start_time"),
            "message_gap_minutes must be": lambda d: d["world"].update(message_gap_minutes=[5, 2]),
            "Turn order must be one of": lambda d: d["rules"].update(turn_order="random"),
        }
        for message, change in cases.items():
            design = rounds_design()
            change(design)
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                validate_design(design)
        normalized = validate_design(rounds_design())
        self.assertEqual(normalized["controller_messages"][0]["after_notes"], None)
        self.assertEqual(normalized["controller_messages"][0]["after_rounds"], 2)

    def test_export_has_the_round_orders_and_the_leaders_service_tiers(self):
        manager = LabManager(ROOT / "data", self.root / "lab", designs_dir=self.root / "designs")
        models = {"agent_01": "decider", "agent_02": "persuader", "agent_03": "persuader", "agent_04": "persuader"}
        created = manager.create({"scenario": "designed", "design": rounds_design(), "mode": "live", "models": models})
        run = manager.runs[created["id"]]
        run.state["rounds"]["orders"] = [["agent_03", "agent_02", "agent_04"], ["agent_02", "agent_04", "agent_03"]]
        run.state["rounds"]["completed"] = 2
        for agent, text in (("agent_01", "Offers?"), ("agent_02", "120"), ("agent_01", "No.")):
            run.action(agent, "post_note", {"content": text})
        for tier in ("flex", "flex", "default"):
            run.record_usage("agent_01", {**USAGE, "service_tier": tier})
        row = next(csv.DictReader(io.StringIO(manager.export_csv())))
        self.assertEqual((row["turn_order"], row["rounds_completed"], row["leader_notes"]), ("rounds", "2", "2"))
        self.assertEqual(row["round_orders"], "seller-2, seller-1, seller-3 | seller-1, seller-3, seller-2")
        self.assertEqual(row["leader_service_tiers"], "default:1 flex:2")


class ErrorReply:
    """An HTTP error with an OpenAI error body."""
    def __init__(self, status, code):
        self.status, self.body = status, json.dumps({"error": {"code": code, "message": "private text sk-test"}}).encode()

    def raise_it(self, *args, **kwargs):
        raise HTTPError("https://api.openai.com/v1/responses", self.status, "error", {}, io.BytesIO(self.body))


class Reply:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, _): return json.dumps(self.data).encode()


class FlexProviderTests(unittest.TestCase):
    profile = {"kind": "openai_responses", "model": "gpt-6-astra", "reasoning_effort": "high",
               "base_url": "https://api.openai.com/v1", "service_tier": "flex"}

    def request(self, outcome, profile=None):
        with patch("swarm_bench.providers.build_opener") as opener:
            if isinstance(outcome, ErrorReply):
                opener.return_value.open.side_effect = outcome.raise_it
            else:
                opener.return_value.open.return_value = Reply(outcome)
            try:
                return completion(profile or self.profile, "sk-test", [{"role": "user", "content": "hi"}], [], 16000)
            finally:
                self.sent = opener.return_value.open.call_args

    def test_flex_is_sent_with_a_long_timeout_and_the_served_tier_is_kept(self):
        reply = {"status": "completed", "service_tier": "flex", "usage": {"input_tokens": 10, "output_tokens": 3},
                 "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        _, usage = self.request(reply)
        request, timeout = self.sent.args[0], self.sent.kwargs["timeout"]
        self.assertEqual(json.loads(request.data)["service_tier"], "flex")
        self.assertEqual(timeout, FLEX_TIMEOUT_SECONDS)
        self.assertEqual(FLEX_TIMEOUT_SECONDS, 18 * 60)
        self.assertEqual(usage["service_tier"], "flex")
        # Without Flex the timeout stays as before.
        self.request(reply, {**self.profile, "service_tier": ""})
        self.assertEqual(self.sent.kwargs["timeout"], 380)
        self.assertNotIn("service_tier", json.loads(self.sent.args[0].data))

    def test_flex_without_capacity_is_a_wait_and_no_credit_is_an_error_to_fix(self):
        with self.assertRaises(ProviderError) as caught:
            self.request(ErrorReply(429, "resource_unavailable"))
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.diagnostic["flex_unavailable"])
        self.assertTrue(_is_rate_limit(caught.exception))
        for code in ("credit_balance_exhausted", "project_spend_limit_exceeded"):
            with self.subTest(code), self.assertRaises(ProviderError) as caught:
                self.request(ErrorReply(429, code), {**self.profile, "service_tier": ""})
            self.assertFalse(caught.exception.retryable)
            self.assertTrue(caught.exception.diagnostic["action_required"])
            self.assertFalse(_is_rate_limit(caught.exception))
            self.assertIn(code, str(caught.exception))
            self.assertNotIn("sk-test", str(caught.exception) + json.dumps(caught.exception.diagnostic))

    def test_profile_accepts_a_tier_only_for_the_responses_format(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = ProviderRegistry(Path(folder) / "models.json")
            base = {"id": "astra", "name": "Astra", "kind": "openai_responses", "base_url": "https://api.openai.com/v1",
                    "model": "gpt-6-astra"}
            registry.save({**base, "service_tier": "flex"})
            for change, message in (({"service_tier": "cheap"}, "Unknown service tier"),
                                    ({"kind": "openai_compatible", "service_tier": "flex"}, "only sent with the OpenAI Responses")):
                with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                    registry.save({**base, **change})


class FlexFallbackTests(RunHelper):
    def test_two_flex_misses_in_a_row_send_one_call_with_standard_processing(self):
        miss = ProviderError("Flex", retryable=True, diagnostic={"http_status": 429, "flex_unavailable": True})
        recorder = Recorder(fail={("buyer", 1): miss, ("buyer", 2): miss})
        profiles = {"agent_01": {"kind": "openai_responses", "service_tier": "flex"},
                    **{a: {"kind": "fake"} for a in ("agent_02", "agent_03", "agent_04")}}
        run = self.run_rounds(rounds_design(after_rounds=1), recorder, profiles=profiles)
        tiers = [c[1]["service_tier"] for c in recorder.calls if c[0] == "buyer"]
        self.assertEqual(tiers, ["flex", "flex", "default", "flex"])
        fallback = [e for e in run.state["events"] if e["kind"] == "service_tier_fallback"]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(run.state["usage"]["agent_01"]["calls"], 2)
        self.assertEqual(run.state["finish_reason"], "action_ended")


if __name__ == "__main__":
    unittest.main()
