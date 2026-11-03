"""Thinking of every provider in the model plan, one whole run each with fake HTTP answers in the provider's own
format: the request asks for the thinking, the run stores the text, the provider's reasoning goes back with the
next call, the saved run returns it, and the Thinking tab shows it. No paid calls."""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request

from swarm_bench.common import ROOT
from swarm_bench.error_help import explain_failure
from swarm_bench.experiment_design import expand, validate_design
from swarm_bench.lab_engine import LabRun
from swarm_bench.lab_server import LabManager
from swarm_bench.leader_signals import leader_columns
from swarm_bench.providers import ProviderError, ProviderRegistry, completion
from swarm_bench.thinking_check import PLAN, stored_thoughts, thinking_request, thinking_tab_html, wire_requests
from swarm_bench.wire_log import WireLog

from test_provider_waits import ok
from test_rounds_and_flex import ErrorReply, QuickEvent, rounds_design

PLAN_PROFILES = {entry["label"]: entry["profile"] for entry in PLAN}
CLAUDE = {"id": "claude", "name": "Claude Opus 5.5", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
          "model": "claude-opus-5-5", "reasoning_effort": "medium", "token_parameter": "max_tokens"}


def thought(name, n):
    """A different summary for every call. The buyer leads, and its text holds a test word for leader_test_terms.
    The < and & check that the tab escapes it."""
    doubt = " Maybe this is a test." if name == "buyer" else ""
    return f"{name} weighs call {n}: price < 120 & fair.{doubt}"


def agent_name(body):
    """The handle from "You are <handle>." at the start of the system prompt, in each provider's format."""
    if isinstance(body.get("system"), str):
        system = body["system"]
    else:
        first = (body.get("messages") or body.get("input"))[0]
        system = first["content"]
    return system.split(".")[0].removeprefix("You are ")


def tool_call_id(name, n):
    return f"call_{name}_{n}"


# Answers in each provider's own format: (name, n, thought, tool, arguments) -> response body.
def responses_answer(name, n, text, tool, arguments):
    return {"status": "completed", "service_tier": "default",
            "output": [{"type": "reasoning", "id": f"rs_{name}_{n}", "encrypted_content": f"enc-{name}-{n}",
                        "summary": [{"type": "summary_text", "text": text}]},
                       {"type": "function_call", "id": f"fc_{name}_{n}", "call_id": tool_call_id(name, n), "name": tool,
                        "arguments": json.dumps(arguments), "status": "completed"}],
            "usage": {"input_tokens": 40, "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 22}}}


def gemini_call(name, n, tool, arguments):
    return {"id": tool_call_id(name, n), "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)},
            "extra_content": {"google": {"thought_signature": f"sig-{name}-{n}"}}}


def gemini_usage():
    # Gemini often leaves the thinking out of completion_tokens. total_tokens still holds it.
    return {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 70}


def gemini_tags(name, n, text, tool, arguments):
    """The summary wrapped in <thought> tags in the content."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": f"<thought>{text}</thought>", "tool_calls": [gemini_call(name, n, tool, arguments)]}}],
        "usage": gemini_usage()}


def gemini_part(name, n, text, tool, arguments):
    """The summary as a content part marked extra_content.google.thought."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text, "extra_content": {"google": {"thought": True}}}],
        "tool_calls": [gemini_call(name, n, tool, arguments)]}}], "usage": gemini_usage()}


def gemini_field(name, n, text, tool, arguments):
    """The summary in reasoning_content beside an empty content."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": text,
        "tool_calls": [gemini_call(name, n, tool, arguments)]}}], "usage": gemini_usage()}


def openrouter_answer(name, n, text, tool, arguments):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "", "reasoning": text,
        "reasoning_details": [{"type": "reasoning.text", "text": text, "format": "unknown", "index": 0}],
        "tool_calls": [{"id": tool_call_id(name, n), "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 30, "completion_tokens_details": {"reasoning_tokens": 22}}}


def anthropic_answer(name, n, text, tool, arguments):
    return {"content": [{"type": "thinking", "thinking": text, "signature": f"sig-{name}-{n}"},
                        {"type": "tool_use", "id": tool_call_id(name, n), "name": tool, "input": arguments}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 40, "output_tokens": 30}}


# What the next call of the same agent must carry back of call n, so the model keeps its own reasoning.
def replay_of(name, n, answer):
    return {responses_answer: f'"encrypted_content": "enc-{name}-{n}"',
            openrouter_answer: json.dumps(openrouter_answer(name, n, thought(name, n), "x", {})
                                          ["choices"][0]["message"]["reasoning_details"]),
            anthropic_answer: f'"signature": "sig-{name}-{n}"'}.get(answer, f'"thought_signature": "sig-{name}-{n}"')


# Every provider of the plan, Gemini in each form its OpenAI layer uses for a summary, and Claude for comparison.
# (profile, answer, fields that ask for thinking, stored kind, label on the Thinking tab)
CASES = {
    "GPT-6 Astra · OpenAI Responses": (
        {**PLAN_PROFILES["GPT-6 Astra"], "reasoning_effort": "medium"}, responses_answer,
        {"reasoning": {"effort": "medium", "summary": "auto"}, "include": ["reasoning.encrypted_content"]},
        "summary", "Summary"),
    **{f"{label} · Gemini, summary as {form}": (
        {**PLAN_PROFILES[label], "reasoning_effort": effort}, answer,
        {"extra_body": {"google": {"thinking_config": {"include_thoughts": True,
                                                       **({"thinking_level": effort} if effort else {})}}}},
        "summary", "Summary")
       for label, effort in (("Gemini 4 Argon", ""), ("Gemini 3.8 Flash", "low"))
       for form, answer in (("thought tags", gemini_tags), ("thought part", gemini_part),
                            ("reasoning_content", gemini_field))},
    "MiMo V2.6 Pro · OpenRouter": (
        PLAN_PROFILES["MiMo V2.6 Pro"], openrouter_answer,
        {"reasoning": {"effort": "medium", "exclude": False}}, "transcript", "Reasoning"),
    "Claude Opus 5.5 · Anthropic Messages": (
        CLAUDE, anthropic_answer,
        {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "medium"}},
        "summary", "Summary"),
}


class PlanProvider:
    """One provider behind HTTP: every agent posts once per call, the buyer signs after "Decide now."."""
    def __init__(self, answer):
        self.answer, self.requests, self.calls, self.lock = answer, [], {}, threading.Lock()

    def open(self, request, timeout=None):
        body = json.loads(request.data)
        name = agent_name(body)
        with self.lock:
            n = self.calls[name] = self.calls.get(name, 0) + 1
            self.requests.append((name, n, request.full_url, body))
        if name == "buyer" and "Decide now." in json.dumps(body):
            tool, arguments = "sign", {"price": 100}
        else:
            tool, arguments = "post_note", {"content": f"{name} #{n}"}
        return ok(self.answer(name, n, thought(name, n), tool, arguments))


class ThinkingOfEveryPlanProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.results = {}
        for label, (profile, answer, *_rest) in CASES.items():
            cls.results[label] = cls.run_case(profile, answer)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_case(cls, profile, answer):
        design = rounds_design(after_rounds=2, sellers=2, stop_on_agent_error=True)
        models = {a["agent_id"]: profile["id"] for a in expand(validate_design(design))}
        work = Path(cls.temp.name) / f"lab{len(cls.results)}"
        run = LabRun({"scenario": "designed", "design": design, "mode": "live", "models": models},
                     ROOT / "data", work / "runs" / "plan-run")
        run.state["provider_profiles"] = {a: dict(profile) for a in run.agents}
        run._provider_keys = {a: "fake-key" for a in run.agents}
        run.pause_requested = QuickEvent()
        provider = PlanProvider(answer)
        with patch("swarm_bench.providers.build_opener") as opener:
            opener.return_value = provider
            worker = threading.Thread(target=run.run_free, daemon=True)
            worker.start()
            worker.join(timeout=30)
        # A fresh manager reads the run from disk, as the interface does after a restart.
        saved = LabManager(ROOT / "data", work)
        details = {a: saved.inspect(run.id, a) for a in run.agents}
        return {"run": run, "provider": provider, "alive": worker.is_alive(), "details": details,
                "names": {a: run.handles.get(a) or a for a in run.agents}}

    def each_case(self, check):
        """check(result, profile, answer, fields, kind, tab_label) once per provider, each as its own subtest."""
        for label, (profile, answer, fields, kind, tab_label) in CASES.items():
            with self.subTest(label):
                check(self.results[label], profile, answer, fields, kind, tab_label)

    def test_the_run_finishes_with_every_call_answered(self):
        def check(result, *_):
            run = result["run"]
            self.assertFalse(result["alive"], "the run did not finish")
            self.assertEqual((run.state["status"], run.state["finish_reason"]), ("complete", "action_ended"))
            self.assertFalse([e for e in run.state["events"] if e["kind"] in ("error", "retry_scheduled")])
            self.assertEqual(result["provider"].calls, {"buyer": 3, "seller-1": 2, "seller-2": 2})
        self.each_case(check)

    def test_every_request_asks_for_the_thinking(self):
        def check(result, profile, _answer, fields, *_):
            for name, n, url, body in result["provider"].requests:
                asks, found, hint = thinking_request(profile, body)
                self.assertTrue(asks, f"{name} call {n}: {hint}")
                self.assertEqual(found, fields)
                if profile["kind"] == "openai_responses":
                    self.assertTrue(url.endswith("/responses"))
                    self.assertFalse(body["store"])
        self.each_case(check)

    def test_the_run_stores_the_thinking_of_every_reply(self):
        def check(result, _profile, _answer, _fields, kind, _label):
            run = result["run"]
            for agent in run.agents:
                name = result["names"][agent]
                replies, thoughts = stored_thoughts(run.histories[agent])
                self.assertEqual(replies, result["provider"].calls[name])
                self.assertEqual(thoughts, [(n, kind, thought(name, n)) for n in range(1, replies + 1)])
            # The thinking is not posted to the channel.
            for note in run.state["notes"]:
                self.assertNotIn("weighs call", note["content"])
        self.each_case(check)

    def test_the_reasoning_goes_back_with_the_next_call(self):
        def check(result, _profile, answer, *_):
            for name, n, _url, body in result["provider"].requests:
                sent = json.dumps(body)
                for earlier in range(1, n):
                    self.assertIn(replay_of(name, earlier, answer), sent, f"{name} call {n} lost call {earlier}")
                if answer is gemini_tags:
                    # The <thought> wrapper stays out of the replayed content.
                    self.assertNotIn("<thought>", sent)
        self.each_case(check)

    def test_the_saved_run_returns_the_thinking(self):
        def check(result, _profile, _answer, _fields, kind, _label):
            for agent, detail in result["details"].items():
                name = result["names"][agent]
                replies, thoughts = stored_thoughts(detail["history"])
                self.assertEqual(replies, result["provider"].calls[name])
                self.assertEqual(thoughts, [(n, kind, thought(name, n)) for n in range(1, replies + 1)])
            # The CSV column for eval awareness reads the leader's thinking.
            histories = {a: d["history"] for a, d in result["details"].items()}
            self.assertEqual(leader_columns(result["run"].snapshot(), histories)["leader_test_terms"], "test")
        self.each_case(check)

    def test_the_thinking_tab_shows_every_reply(self):
        if thinking_tab_html({"history": []}) is None:
            self.skipTest("Node is not installed, so the Thinking tab code of app.js cannot run")
        def check(result, _profile, _answer, _fields, _kind, tab_label):
            for agent, detail in result["details"].items():
                name = result["names"][agent]
                shown = thinking_tab_html(detail)
                self.assertNotIn("No reasoning text came back", shown)
                self.assertNotIn("replies included reasoning text", shown)
                for n in range(1, result["provider"].calls[name] + 1):
                    self.assertIn(f"Reply {n} · {tab_label}", shown)
                    self.assertIn(html.escape(thought(name, n), quote=False), shown)
                self.assertNotIn(thought(name, 1), shown, "the text must be escaped")
        self.each_case(check)


class ThinkingSettingsTests(unittest.TestCase):
    def test_off_and_missing_settings_are_named(self):
        gemini = PLAN_PROFILES["Gemini 3.8 Flash"]
        self.assertEqual(thinking_request(gemini, {"reasoning_effort": "none"})[0], False)
        mimo = {**PLAN_PROFILES["MiMo V2.6 Pro"], "reasoning_effort": ""}
        asks, fields, hint = thinking_request(mimo, {"model": "xiaomi/mimo-v2.6-pro"})
        self.assertEqual((asks, fields), (False, {}))
        self.assertIn("Choose an effort", hint)
        self.assertFalse(thinking_request(mimo, {"reasoning": {"effort": "medium", "exclude": True}})[0])
        self.assertFalse(thinking_request(PLAN_PROFILES["GPT-6 Astra"], {"reasoning": {"effort": "low"}})[0])

    def test_off_is_refused_for_the_plan_models_that_always_think(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = ProviderRegistry(Path(folder) / "models.json")
            for label in ("GPT-6 Astra", "Gemini 4 Argon", "Gemini 3.8 Flash"):
                with self.subTest(label), self.assertRaisesRegex(ValueError, "cannot turn thinking off"):
                    registry.save({**PLAN_PROFILES[label], "reasoning_effort": "none"})
            registry.save({**PLAN_PROFILES["MiMo V2.6 Pro"], "reasoning_effort": "none"})
            self.assertEqual([p["model"] for p in registry.public()], ["xiaomi/mimo-v2.6-pro"])

    def test_the_model_picker_offers_the_plan_profiles(self):
        """The presets of the Models page fill the same settings as the plan profiles of the smoke test."""
        source = (ROOT / "swarm_bench" / "web" / "app.js").read_text(encoding="utf-8")
        presets = [json.loads(re.sub(r'([{,])(\w+):', r'\1"\2":', line.strip().rstrip(",")))
                   for line in source.splitlines() if line.startswith('  {group:"')]
        for label, plan in PLAN_PROFILES.items():
            with self.subTest(label):
                preset = next(p for p in presets if p["model"] == plan["model"])
                for field in ("kind", "base_url", "token_parameter", "reasoning_effort", "key_env"):
                    self.assertEqual(preset[field], plan[field], field)

    def test_an_unverified_openai_organization_is_named(self):
        """OpenAI refuses the summary with HTTP 400 when the organization is not verified."""
        reply = ErrorReply(400, "unsupported_value")
        reply.body = json.dumps({"error": {"type": "invalid_request_error", "code": "unsupported_value",
                                           "param": "reasoning.summary", "message": "Your organization must be "
                                           "verified to generate reasoning summaries. sk-test"}}).encode()
        with patch("swarm_bench.providers.build_opener") as opener:
            opener.return_value.open.side_effect = reply.raise_it
            with self.assertRaises(ProviderError) as caught:
                completion(PLAN_PROFILES["GPT-6 Astra"], "sk-test", [{"role": "user", "content": "hi"}], [], 1000)
        error = caught.exception
        self.assertTrue(error.diagnostic["summary_verification"])
        self.assertFalse(error.retryable)
        self.assertNotIn("sk-test", str(error) + json.dumps(error.diagnostic))
        title, what, todo = explain_failure(str(error), error.diagnostic, PLAN_PROFILES["GPT-6 Astra"],
                                            run_ends=True, resumable=False)
        self.assertEqual(title, "OpenAI sends no reasoning summary to this organization")
        self.assertIn("Verify the organization", todo)
        # Any other 400 keeps the general explanation, also one that names reasoning.summary without verification.
        unsupported = ErrorReply(400, "unsupported_parameter")
        unsupported.body = json.dumps({"error": {"param": "reasoning.summary", "code": "unsupported_parameter",
                                                 "message": "reasoning.summary is not supported with this model."}}).encode()
        for other in (ErrorReply(400, "invalid_request_error"), unsupported):
            with patch("swarm_bench.providers.build_opener") as opener:
                opener.return_value.open.side_effect = other.raise_it
                with self.assertRaises(ProviderError) as caught:
                    completion(PLAN_PROFILES["GPT-6 Astra"], "sk-test", [{"role": "user", "content": "hi"}], [], 1000)
            self.assertNotIn("summary_verification", caught.exception.diagnostic)
            self.assertEqual(caught.exception.diagnostic["http_status"], 400)

    def test_request_bodies_are_read_back_from_the_wire_log(self):
        first = {"model": "gpt-6-astra", "reasoning": {"summary": "auto"}, "input": "key sk-secret-123"}
        second = {"messages": [{"role": "user", "content": "a ``` fence"}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "wire_log.md"
            log = WireLog(path, "r")
            for request_id, body in (("agent_01/1", first), ("agent_02/1", second)):
                exchange = log.exchange(request_id)
                exchange.sent(Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                      headers={"Authorization": "Bearer sk-secret-123"}), 60, "sk-secret-123")
                exchange.received(200, "OK", {}, b'{"status": "completed"}')
            self.assertEqual(wire_requests(path), [("agent_01/1", {**first, "input": "key [REDACTED]"}),
                                                   ("agent_02/1", second)])
            self.assertEqual(wire_requests(Path(folder) / "missing.md"), [])


class SmokeScriptTests(unittest.TestCase):
    """scripts/smoke_thinking.py against a local server in the Responses format: the script runs, reads the
    wire log, the saved run and the Thinking tab, and reports. The real check needs real keys."""
    def test_the_smoke_script_passes_a_provider_that_returns_summaries_and_fails_one_without(self):
        calls = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = "lead" if "Mara Lind, Office" in body["input"][0]["content"] else "colleague"
                n = calls[body["model"], name] = calls.get((body["model"], name), 0) + 1
                decide = name == "lead" and "place the lunch order now" in json.dumps(body)
                tool, arguments = ("order_lunch", {"option": "Bowl Bar", "amount": 136}) if decide else (
                    "post_note", {"content": f"{name} {n}"})
                summary = [] if body["model"] == "silent" else [{"type": "summary_text", "text": f"{name} weighs {n}"}]
                answer = {"status": "completed", "usage": {"input_tokens": 9, "output_tokens": 4}, "output": [
                    {"type": "reasoning", "id": f"rs{n}", "encrypted_content": "enc", "summary": summary},
                    {"type": "function_call", "call_id": f"{name}{n}", "name": tool, "arguments": json.dumps(arguments)}]}
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                registry = ProviderRegistry(Path(folder) / "models.json")
                local = {"kind": "openai_responses", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                         "reasoning_effort": "low"}
                registry.save({**local, "id": "summaries", "name": "Summaries", "model": "astra-like"})
                registry.save({**local, "id": "silent", "name": "Silent", "model": "silent"})
                result = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_thinking.py"), "--work-dir", folder,
                                         "--profile", "summaries", "silent"], capture_output=True, text=True,
                                        encoding="utf-8", timeout=120, env={**os.environ, "NO_PROXY": "127.0.0.1"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ok   all 3 requests asked for thinking", result.stdout)
        self.assertIn("ok   Mara Lind: 2 of 2 replies with thinking text (summary)", result.stdout)
        self.assertIn("FAIL Mara Lind: 0 of 2 replies with thinking text (none)", result.stdout)
        self.assertRegex(result.stdout, r"passed +Summaries +\(run ")
        self.assertRegex(result.stdout, r"failed +Silent +\(run ")


if __name__ == "__main__":
    unittest.main()
