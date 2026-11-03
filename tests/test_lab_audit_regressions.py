"""Regressions for the five issues reproduced by the September 2026 audit."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_bench.common import ROOT, read_json
from swarm_bench.lab_engine import LabRun, DEFAULT_COMMON_PROMPT
from swarm_bench.lab_server import LabManager
from swarm_bench.lab_storage import atomic_json
from swarm_bench.providers import ProviderError


def call(name, args, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def reply(*calls, text=""):
    return {"role": "assistant", "content": text, **({"tool_calls": list(calls)} if calls else {})}, {"input_tokens": 10, "output_tokens": 4}


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = LabManager(ROOT / "data", self.root)
        self.profile = {"id": "fake", "name": "Fake", "kind": "openai_compatible",
                        "base_url": "http://127.0.0.1:1234/v1", "model": "model-A", "reasoning_effort": "none"}
        self.manager.registry.save(self.profile)

    def create(self, n=2, **changes):
        config = {"agent_count": n, "mode": "live", "call_limit": 4,
                  "models": {f"agent_{i:02d}": "fake" for i in range(1, n + 1)}, "board_delivery": "tool_only", **changes}
        state = self.manager.create(config)
        return self.manager.runs[state["id"]]

    def test_board_and_file_in_one_response_do_not_count_new_exposure(self):
        run = self.create()
        run.action("agent_02", "post_note", {"content": "Please open your file."})
        contexts = []
        def model(_profile, _key, messages, *_args):
            contexts.append(copy.deepcopy(messages))
            if len(contexts) == 1:
                return reply(call("read_board", {}, "board"), call("read_file", {"path": "notes.json"}, "file"))
            return reply(call("read_file", {"path": "notes.json"}, "file-next"))
        with patch("swarm_bench.lab_engine.completion", model):
            run.live_call("agent_01")
            self.assertEqual(run.state["reads"][0]["exposed_note_ids"], [])
            self.assertEqual(run.state["reads"][0]["tool_call_id"], "file")
            self.assertEqual(run.state["exposures"]["agent_01"], [])
            self.assertEqual(run.metrics()["after_peer_exposure"], 0)
            run.live_call("agent_01")
        self.assertNotIn("Please open your file.", json.dumps(contexts[0]))
        self.assertIn("Please open your file.", json.dumps(contexts[1]))
        self.assertEqual(run.state["reads"][1]["exposed_note_ids"], [1])
        self.assertNotEqual(run.state["reads"][0]["request_id"], run.state["reads"][1]["request_id"])
        self.assertEqual(run.metrics()["before_peer_exposure"], 1)  # First breach remains before exposure.

    def test_new_peer_notes_during_generation_do_not_enter_its_decision(self):
        run = self.create()
        run.action("agent_02", "post_note", {"content": "old note"})
        with patch("swarm_bench.lab_engine.completion", return_value=reply(call("read_board", {}, "board"))):
            run.live_call("agent_01")
        entered, release = threading.Event(), threading.Event()
        captured = []
        def model(_profile, _key, messages, *_args):
            captured.append(messages)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Model test timed out")
            return reply(call("read_board", {}, "board-latest"), call("read_file", {"path": "notes.json"}, "file"))
        with patch("swarm_bench.lab_engine.completion", model):
            worker = threading.Thread(target=run.live_call, args=("agent_01",))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                run.action("agent_02", "post_note", {"content": "new note after dispatch"})
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("new note after dispatch", json.dumps(captured))
        self.assertEqual(run.state["reads"][0]["exposed_note_ids"], [1])
        boards = [e for e in run.state["events"] if e["kind"] == "board_read"]
        self.assertEqual(boards[-1]["note_ids"], [1, 2])

    def test_truncation_counts_tokens_and_cannot_be_resumed_past_budget(self):
        for kind in ("openai_compatible", "anthropic"):
            with self.subTest(kind=kind):
                self.manager.registry.save({**self.profile, "kind": kind, "reasoning_effort": ""})
                run = self.create(n=1, call_limit=1)
                raw = ({"stop_reason": "max_tokens", "content": [], "usage": {"input_tokens": 20, "output_tokens": 128}}
                       if kind == "anthropic" else {"choices": [{"finish_reason": "length", "message": {"content": "unfinished"}}],
                                                     "usage": {"prompt_tokens": 20, "completion_tokens": 128}})
                with patch("swarm_bench.providers.build_opener") as factory:
                    factory.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(raw).encode()
                    self.manager.control(run.id, "play")
                    self.manager.workers[run.id].join(3)
                    for _ in range(3):
                        with self.assertRaisesRegex(ValueError, "Call limit"):
                            self.manager.control(run.id, "play")
                    self.assertEqual(factory.return_value.open.call_count, 1)
                usage = run.state["usage"]["agent_01"]
                self.assertEqual((usage["calls"], usage["failed_calls"], usage["successful_calls"]), (1, 1, 0))
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (20, 128))
                self.assertEqual(usage["usage_unavailable_calls"], 0)
                self.assertEqual(run.histories["agent_01"][-1]["role"], "user")
                saved = LabManager(ROOT / "data", self.root).snapshot(run.id)
                self.assertEqual(saved["usage"]["agent_01"], usage)

    def test_timeout_gives_the_attempt_back_and_missing_profile_uses_none(self):
        # Waiting for a provider says nothing about the model: a timeout never uses up the call limit.
        run = self.create(n=1, call_limit=1)
        with patch("swarm_bench.providers.build_opener") as factory:
            factory.return_value.open.side_effect = TimeoutError()
            for _ in range(2):
                with self.assertRaises(ProviderError):
                    run.live_call("agent_01")
            self.assertEqual(factory.return_value.open.call_count, 2)
        usage = run.state["usage"]["agent_01"]
        self.assertEqual(usage["calls"], 0)
        self.assertEqual(usage["failed_calls"], 2)
        self.assertEqual(usage["usage_unavailable_calls"], 2)
        self.assertEqual(run.state["token_budget"]["accounted"], 0)
        missing = self.create(n=1, models={"agent_01": "missing"})
        with patch("swarm_bench.lab_engine.completion") as model:
            with self.assertRaises(ProviderError):
                self.manager.control(missing.id, "play")
            self.assertEqual(missing.state["usage"]["agent_01"]["calls"], 0)
            model.assert_not_called()

    def test_attempt_is_saved_before_dispatch_and_error_resume_uses_remaining_budget(self):
        run = self.create(n=1, call_limit=2)
        def failed(*_args):
            checkpoint = read_json(run.folder / "checkpoint.json")
            self.assertEqual(checkpoint["state"]["usage"]["agent_01"]["calls"], 1)
            raise ProviderError("temporary failure")
        with patch("swarm_bench.lab_engine.completion", failed):
            self.manager.control(run.id, "play")
            self.manager.workers[run.id].join(3)
        self.assertEqual(run.state["status"], "error")
        with patch("swarm_bench.lab_engine.completion", return_value=reply(text="done")) as model:
            self.manager.control(run.id, "play")
            self.manager.workers[run.id].join(3)
            self.assertEqual(model.call_count, 1)
        self.assertEqual(run.state["usage"]["agent_01"]["calls"], 2)
        self.assertEqual(run.state["usage"]["agent_01"]["failed_calls"], 1)
        self.assertEqual(run.state["usage"]["agent_01"]["successful_calls"], 1)

    def test_partial_usage_is_preserved_even_for_an_invalid_response(self):
        run = self.create(n=1)
        with patch("swarm_bench.providers.build_opener") as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({
                "choices": [], "usage": {"prompt_tokens": 23}}).encode()
            with self.assertRaises(ProviderError):
                run.live_call("agent_01")
        usage = run.state["usage"]["agent_01"]
        self.assertEqual(usage["input_tokens"], 23)
        self.assertEqual(usage["usage_unavailable_calls"], 1)
        error = next(e for e in run.state["events"] if e["kind"] == "model_error")
        self.assertIsNone(error["usage"]["output_tokens"])

    def test_profile_snapshot_survives_edits_and_secrets_never_leave_memory(self):
        run = self.create()
        # Edits before the first start are allowed, and affect every agent consistently.
        self.manager.registry.save({**self.profile, "model": "model-at-start", "api_key": "secret-for-frozen-run-test"})
        seen = []
        def model(profile, key, *_args):
            seen.append((copy.deepcopy(profile), key))
            return reply(text="done")
        with patch("swarm_bench.lab_engine.completion", model):
            run.live_call("agent_01")
            self.manager.registry.save({**self.profile, "model": "model-B", "base_url": "http://127.0.0.1:5678/v1",
                                        "reasoning_effort": "high", "api_key": "replacement-key-for-test"})
            run.live_call("agent_02")
            newer = self.create(n=1)
            newer.live_call("agent_01")
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0][0]["model"], "model-at-start")
        self.assertEqual(seen[2][0]["model"], "model-B")
        exported = self.manager.export(run.id)
        self.assertEqual(exported["provider_profiles"]["agent_02"]["model"], "model-at-start")
        self.assertNotIn("secret-for-frozen-run-test", json.dumps(exported))
        for path in self.root.rglob("*.json"):
            if path.name == ".provider-secrets.json":continue
            self.assertNotIn("secret-for-frozen-run-test", path.read_text())
            self.assertNotIn("replacement-key-for-test", path.read_text())

    def test_interrupted_checkpoint_keeps_last_consistent_state_and_history(self):
        run = self.create(n=1)
        before = self.manager.export(run.id)
        run.state["question"] = "uncommitted question"
        run.histories["agent_01"].append({"role": "assistant", "content": "uncommitted response"})
        with patch("swarm_bench.lab_storage.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                run.persist()
        restored = LabManager(ROOT / "data", self.root).export(run.id)
        self.assertEqual(restored["question"], before["question"])
        self.assertEqual(restored["participants"], before["participants"])
        self.assertEqual(list(run.folder.glob(".*.tmp")), [])

    def test_failure_after_checkpoint_commit_ignores_stale_or_broken_mirrors(self):
        run = self.create(n=1)
        run.state["question"] = "committed question"
        run.histories["agent_01"].append({"role": "assistant", "content": "committed response"})
        def fail_mirrors(path, value):
            if Path(path).name != "checkpoint.json":
                Path(path).write_text('{"broken":')
                raise OSError("mirror write interrupted")
            atomic_json(path, value)
        with patch("swarm_bench.lab_storage.atomic_json", fail_mirrors):
            run.persist()
        restored = LabManager(ROOT / "data", self.root).export(run.id)
        self.assertEqual(restored["question"], "committed question")
        self.assertEqual(restored["participants"]["agent_01"]["history"][-1]["content"], "committed response")

    def test_interruption_between_action_and_tool_result_cannot_commit_half_a_response(self):
        run = self.create(n=1)
        original = run.action
        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertEqual(len(run.state["reads"]), 1)
            raise OSError("interrupted after action, before tool result")
        with patch.object(run, "action", interrupted), patch("swarm_bench.lab_engine.completion", return_value=reply(call("read_file", {"path": "notes.json"}, "read"))):
            with self.assertRaises(OSError):
                run.live_call("agent_01")
        exported = LabManager(ROOT / "data", self.root).export(run.id)
        self.assertEqual(exported["reads"], [])
        self.assertFalse(any(m["role"] == "assistant" for m in exported["participants"]["agent_01"]["history"]))
        self.assertEqual(exported["usage"]["agent_01"]["calls"], 1)

    def test_legacy_archive_with_broken_history_still_exports_surviving_observations(self):
        run = self.create(n=1)
        run.action("agent_01", "read_file", {"path": "notes.json"})
        (run.folder / "checkpoint.json").unlink()  # Simulate a pre-checkpoint-format archive.
        (run.folder / "histories.json").write_text('{"agent_01":[')
        exported = LabManager(ROOT / "data", self.root).export(run.id)
        self.assertEqual(len(exported["reads"]), 1)
        self.assertTrue(exported["archive_warnings"])
        self.assertEqual(exported["participants"]["agent_01"]["history"], [])

    def test_custom_solo_defaults_and_explicit_prompts(self):
        for override in ({}, {"common_prompt": None}):
            run = self.create(n=1, scenario="custom", custom_question="Work alone.", **override)
            self.assertIn("Your agent id: agent_01", run.prompt("agent_01"))
            self.assertNotIn("other participants", run.prompt("agent_01"))
        for common in ("", DEFAULT_COMMON_PROMPT, "Exactly my custom instruction."):
            run = self.create(n=1, scenario="custom", custom_question="Work alone.", common_prompt=common)
            self.assertEqual(run.config["common_prompt"], common)
            self.assertIn(common, run.prompt("agent_01"))
        group = self.create(scenario="custom", custom_question="Work together.")
        self.assertIn(DEFAULT_COMMON_PROMPT, group.prompt("agent_01"))

    def test_continue_policy_uses_the_budget_without_inventing_peer_notes(self):
        run = self.create(call_limit=3, idle_policy="continue", idle_wait_seconds=0)
        with patch("swarm_bench.lab_engine.completion", return_value=reply(text="done")) as model:
            run.run_free()
        self.assertEqual(model.call_count, 6)
        self.assertEqual(run.state["finish_reason"], "call_limit")
        self.assertTrue(all(u["calls"] == 3 for u in run.state["usage"].values()))
        continuations = [e for e in run.state["events"] if e["kind"] == "continuation_requested"]
        self.assertEqual(len(continuations), 4)
        self.assertTrue(all(e["origin"] == "controller" for e in continuations))
        self.assertEqual(run.state["notes"], [])
        self.assertTrue(all(not e["exposed_note_ids"] for e in run.state["events"] if e["kind"] == "model_request"))
        with self.assertRaises(ValueError):
            run.live_call("agent_01")

    def test_continue_policy_does_not_wait_for_other_agents_turns(self):
        run = self.create(call_limit=3, idle_policy="continue", idle_wait_seconds=0)
        release_slow, fast_done = threading.Event(), threading.Event()
        fast_calls = []
        def model(_profile, _key, messages, *_args):
            if messages[0]["content"].startswith("You are agent_02,"):
                if not release_slow.wait(3):
                    raise RuntimeError("Slow agent was not released")
            else:
                fast_calls.append(1)
                if len(fast_calls) == 3:
                    fast_done.set()
            return reply(text="done")
        with patch("swarm_bench.lab_engine.completion", model):
            worker = threading.Thread(target=run.run_free)
            worker.start()
            try:
                self.assertTrue(fast_done.wait(2))
                self.assertFalse(release_slow.is_set())
            finally:
                release_slow.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(run.state["status"], "complete")

    def test_pause_and_stop_interrupt_the_continuation_wait(self):
        for action in ("pause", "stop"):
            with self.subTest(action=action):
                run = self.create(n=1, call_limit=3, idle_policy="continue", idle_wait_seconds=60)
                with patch("swarm_bench.lab_engine.completion", return_value=reply(text="done")) as model:
                    self.manager.control(run.id, "play")
                    deadline = time.monotonic() + 2
                    while run.state["agent_status"]["agent_01"] != "waiting" and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(run.state["agent_status"]["agent_01"], "waiting")
                    self.manager.control(run.id, action)
                    self.manager.workers[run.id].join(2)
                    self.assertFalse(self.manager.workers[run.id].is_alive())
                    self.assertEqual(model.call_count, 1)
                self.assertEqual(run.state["status"], "paused" if action == "pause" else "stopped")
                self.assertFalse(any(e["kind"] == "continuation_requested" for e in run.state["events"]))


if __name__ == "__main__":
    unittest.main()
