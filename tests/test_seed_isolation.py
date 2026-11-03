"""The run seed never reaches a provider in a designed run (Group Spending), for any provider format.

Only the socket is replaced: providers.completion() builds the real request and an extra urllib
handler answers it, so the body checked here is the body a provider would receive.
"""
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse
from urllib.request import BaseHandler, addinfourl

from swarm_bench import providers
from swarm_bench.common import read_json
from swarm_bench.experiment_design import expand, validate_design
from swarm_bench.lab_server import LabManager

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "model": "m",
                  "reasoning_effort": "low"},
    "openai_responses": {"kind": "openai_responses", "base_url": "https://api.openai.com/v1", "model": "m",
                         "reasoning_effort": "low"},
    "gemini": {"kind": "openai_compatible", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3-m", "reasoning_effort": "low"},
    "openrouter": {"kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "v/m",
                   "reasoning_effort": "low"},
}


def reply(host, first):
    """The first call reads the board, the next one answers with text."""
    if host == "api.anthropic.com":
        return {"content": [{"type": "tool_use", "id": "t1", "name": "read_board", "input": {}}] if first
                else [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    if host == "api.openai.com":
        return {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}, "output":
                [{"type": "function_call", "call_id": "t1", "name": "read_board", "arguments": "{}"}] if first
                else [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
    message = ({"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "read_board", "arguments": "{}"}}]} if first
        else {"role": "assistant", "content": "ok"})
    return {"choices": [{"finish_reason": "stop", "message": message}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class FakeProvider(BaseHandler):
    handler_order = 100

    def __init__(self, bodies):
        self.bodies = bodies

    def https_open(self, request):
        # The host from the URL: a proxy from the environment would replace request.host.
        host = urlparse(request.full_url).hostname
        body = json.loads(request.data)
        self.bodies.append(body)
        raw = json.dumps(reply(host, len(body.get("messages", body.get("input", []))) <= 2)).encode()
        headers = Message()
        headers["Content-Type"] = "application/json"
        response = addinfourl(io.BytesIO(raw), headers, request.full_url, 200)
        response.msg = response.reason = "OK"
        return response


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


class SeedIsolationTests(unittest.TestCase):
    def requests(self, name, seed):
        design = read_json(ROOT / "designs/group-spending.json")
        agents = [r["agent_id"] for r in expand(validate_design(design))]
        bodies = []
        real = providers.build_opener
        with tempfile.TemporaryDirectory() as work, \
                patch.object(providers, "build_opener", lambda *h: real(*h, FakeProvider(bodies))):
            manager = LabManager(ROOT / "data", Path(work) / "lab")
            manager.registry.save({"id": name, "name": name, "api_key": "sk-fake-1234", **PROFILES[name]})
            state = manager.create({"scenario": "designed", "design": design, "mode": "live", "seed": seed,
                                    "models": {a: name for a in agents}})
            self.assertEqual(state["config"]["seed"], seed)
            run = manager.runs[state["id"]]
            run.live_call(agents[0])
            run.live_call(agents[0])
            log = manager.wire_log(state["id"]).decode("utf-8")
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("seed", log.lower())
        return bodies

    def test_no_seed_in_any_provider_request(self):
        for name in PROFILES:
            with self.subTest(provider=name):
                with_42 = self.requests(name, 42)
                self.assertFalse(any("seed" in keys(body) for body in with_42))
                # Another seed changes no byte of what the provider receives.
                self.assertEqual(with_42, self.requests(name, 7))


if __name__ == "__main__":
    unittest.main()
