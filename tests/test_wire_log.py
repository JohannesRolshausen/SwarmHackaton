import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarm_bench.lab_server import LabManager
from swarm_bench.providers import ProviderError, completion
from swarm_bench.wire_log import WireLog

KEY = "sk-test-wire-secret-1234"
ROOT = Path(__file__).resolve().parents[1]


class Provider(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Provider.received.append(body)
        reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "hi ```x``` " + KEY}}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        self.send_response(Provider.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class WireLogTests(unittest.TestCase):
    def setUp(self):
        Provider.received, Provider.status = [], 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.profile = {"kind": "openai_compatible", "model": "m",
                        "base_url": f"http://127.0.0.1:{self.server.server_port}/v1"}
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "wire_log.md"
        self.log = WireLog(self.path, "run-1")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_logs_exact_request_and_response_without_credentials(self):
        messages = [{"role": "user", "content": "question"}]
        msg, _ = completion(self.profile, KEY, messages, [], 50, None, self.log.exchange("agent_01/1"))
        text = self.path.read_text(encoding="utf-8")
        sent = Provider.received[0]
        self.assertIn(hashlib.sha256(sent).hexdigest(), text)
        self.assertIn(json.dumps(json.loads(sent), indent=2), text)
        self.assertIn("## → agent_01/1 · request", text)
        self.assertIn("## ← agent_01/1 · response 200 OK", text)
        self.assertIn("POST " + self.profile["base_url"] + "/chat/completions", text)
        self.assertIn("Authorization: [REDACTED]", text)
        self.assertIn(f"Content-length: {len(sent)}", text)
        self.assertIn("Host: 127.0.0.1", text)
        self.assertNotIn(KEY, text)
        self.assertIn(KEY, msg["content"])  # Only the log is redacted, not what the run sees.
        self.assertIn("````json", text)  # Backticks in the body widen the fence.

    def test_logs_http_error_body(self):
        Provider.status = 429
        with self.assertRaises(ProviderError):
            completion(self.profile, KEY, [{"role": "user", "content": "q"}], [], 50, None,
                       self.log.exchange("agent_02/4"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("## ← agent_02/4 · response HTTP error 429", text)
        self.assertIn('"finish_reason": "stop"', text)
        self.assertNotIn(KEY, text)

    def test_logs_connection_failure(self):
        closed = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        closed.server_close()  # Nothing listens on this port any more.
        profile = {**self.profile, "base_url": f"http://127.0.0.1:{closed.server_port}/v1"}
        with self.assertRaises(ProviderError):
            completion(profile, KEY, [{"role": "user", "content": "q"}], [], 50, None,
                       self.log.exchange("agent_03/1"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("## → agent_03/1 · request", text)
        self.assertIn("## ← agent_03/1 · connection failed", text)

    def test_lab_run_writes_log_that_manager_serves(self):
        manager = LabManager(ROOT / "data", Path(self.temp.name) / "lab")
        manager.registry.save({"id": "wire", "name": "Wire", "kind": "openai_compatible", "model": "m",
                               "base_url": self.profile["base_url"], "api_key": KEY})
        state = manager.create({"scenario": "custom", "agent_count": 2, "mode": "live", "call_limit": 2,
                                "models": {"agent_01": "wire", "agent_02": "wire"},
                                "custom_question": "Say hi.", "answer_policy": "none"})
        manager.runs[state["id"]].live_call("agent_01")
        text = manager.wire_log(state["id"]).decode("utf-8")
        self.assertIn("## → agent_01/1 · request", text)
        self.assertIn("## ← agent_01/1 · response 200 OK", text)
        self.assertIn(hashlib.sha256(Provider.received[0]).hexdigest(), text)
        self.assertNotIn(KEY, text)
        restored = LabManager(ROOT / "data", Path(self.temp.name) / "lab")
        self.assertEqual(restored.wire_log(state["id"]).decode("utf-8"), text)
        with self.assertRaises(ValueError):
            manager.wire_log("does-not-exist")


if __name__ == "__main__":
    unittest.main()
