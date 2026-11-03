"""Provider errors pause a run, they never end it. Each kind goes through a fake HTTP provider: the real
provider adapter reads it, the run waits and tries again, and finishes by itself. No paid calls."""
import http.client
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from swarm_bench.providers import ProviderError, completion

from test_rounds_and_flex import RunHelper, call, rounds_design

PROFILE = {"name": "Fake", "kind": "openai_compatible", "model": "fake-model", "base_url": "https://fake.test/v1"}


class Body:
    """An HTTP 200 answer whose body is given as bytes, or whose read raises."""
    def __init__(self, raw=b"", error=None):
        self.raw, self.error, self.status, self.reason, self.headers = raw, error, 200, "OK", {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _=None):
        if self.error:
            raise self.error
        return self.raw


def ok(data):
    return Body(json.dumps(data).encode())


def http_error(status):
    def fail(*_args, **_kwargs):
        raise HTTPError("https://fake.test/v1/chat/completions", status, "error", {},
                        io.BytesIO(json.dumps({"error": {"code": status, "message": "private text"}}).encode()))
    return fail


def raises(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


# Every provider error that must only pause the run, with the wait reason the run logs for it.
WAITS = {
    "timeout": (raises(TimeoutError()), "timeout"),
    "HTTP 429": (http_error(429), "rate_limit"),
    "HTTP 408": (http_error(408), "server_error"),
    **{f"HTTP {s}": (http_error(s), "server_error") for s in (500, 501, 502, 503, 504, 520, 522, 524, 529)},
    "connection refused": (raises(URLError(ConnectionRefusedError())), "network"),
    "connection reset": (raises(ConnectionResetError()), "network"),
    "server closed the connection": (raises(http.client.RemoteDisconnected("closed")), "network"),
    "answer broke off": (lambda *a, **k: Body(error=http.client.IncompleteRead(b'{"choi')), "network"),
    "no valid HTTP": (raises(http.client.BadStatusLine("garbage")), "network"),
    "HTML instead of JSON": (lambda *a, **k: Body(b"<html>502 Bad Gateway</html>"), "network"),
    "JSON cut short": (lambda *a, **k: Body(b'{"choices": [{"mess'), "network"),
    "JSON error 503 in HTTP 200": (lambda *a, **k: ok({"error": {"code": 503, "message": "x"}}), "server_error"),
    "JSON error 524 in HTTP 200": (lambda *a, **k: ok({"error": {"code": 524, "message": "x"}}), "server_error"),
    "JSON error 429 in HTTP 200": (lambda *a, **k: ok({"error": {"code": 429, "message": "x"}}), "rate_limit"),
    "JSON error without code": (lambda *a, **k: ok({"error": {"message": "upstream failed"}}), "server_error"),
    "empty choices": (lambda *a, **k: ok({"choices": []}), "incomplete"),
    "null message": (lambda *a, **k: ok({"choices": [{"message": None}]}), "incomplete"),
    "null body": (lambda *a, **k: Body(b"null"), "incomplete"),
}

# Errors only the operator can fix stop the agent, and with stop_on_agent_error the run.
STOPS = {
    "wrong key (HTTP 401)": http_error(401),
    "unknown model (HTTP 404)": http_error(404),
    "invalid request (HTTP 400)": http_error(400),
    "answer at the token limit": lambda *a, **k: ok({"choices": [{"finish_reason": "length",
                                                                  "message": {"content": "unfinished"}}]}),
}


class FakeProvider:
    """A scripted model behind HTTP: everyone posts once per call, the buyer signs after "Decide now.".

    The first two calls of seller-1 get the given failure instead."""
    def __init__(self, failure, failing="seller-1", times=2):
        self.failure, self.failing, self.left, self.calls = failure, failing, times, []

    def open(self, request, timeout=None):
        messages = json.loads(request.data)["messages"]
        name = messages[0]["content"].split(".")[0].removeprefix("You are ")
        self.calls.append(name)
        if name == self.failing and self.left:
            self.left -= 1
            return self.failure(request, timeout=timeout)
        n = len(self.calls)
        if name == "buyer" and any(m.get("content") == "Decide now." for m in messages):
            tool = call("sign", {"price": 100}, f"s{n}")
        else:
            tool = call("post_note", {"content": f"{name} #{n}"}, f"p{n}")
        return ok({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "",
                                                                          "tool_calls": [tool]}}],
                   "usage": {"prompt_tokens": 4, "completion_tokens": 2}})


class ProviderWaitTests(RunHelper):
    def run_with(self, provider):
        with patch("swarm_bench.providers.build_opener") as opener:
            opener.return_value = provider
            return self.run_rounds(rounds_design(after_rounds=1, stop_on_agent_error=True), completion,
                                   profiles={a: PROFILE for a in ("agent_01", "agent_02", "agent_03", "agent_04")},
                                   call_limit=3)

    def test_every_temporary_provider_error_only_pauses_the_run(self):
        for label, (failure, reason) in WAITS.items():
            with self.subTest(label):
                provider = FakeProvider(failure)
                run = self.run_with(provider)
                self.assertFalse([e for e in run.state["events"] if e["kind"] == "error"])
                self.assertEqual((run.state["status"], run.state["finish_reason"]), ("complete", "action_ended"))
                waits = [e["reason"] for e in run.state["events"] if e["kind"] == "retry_scheduled"]
                self.assertEqual(waits, [reason, reason])
                # Seller-1 asked three times: two waits, given back, and one call that counted.
                self.assertEqual(provider.calls.count("seller-1"), 3)
                self.assertEqual(run.state["usage"]["agent_02"]["calls"], 1)
                self.assert_rounds(run, 1)

    def test_errors_only_the_operator_can_fix_still_stop(self):
        for label, failure in STOPS.items():
            with self.subTest(label):
                run = self.run_with(FakeProvider(failure))
                self.assertEqual((run.state["status"], run.state["finish_reason"]), ("error", "agent_error"))
                self.assertEqual(run.state["agent_status"]["agent_02"], "error")
                self.assertFalse([e for e in run.state["events"] if e["kind"] == "retry_scheduled"])

    def test_a_failed_or_incomplete_responses_call_is_a_wait(self):
        profile = {"kind": "openai_responses", "model": "fake", "base_url": "https://fake.test/v1"}
        for status in ("failed", "incomplete"):
            with self.subTest(status), patch("swarm_bench.providers.build_opener") as opener:
                opener.return_value.open.return_value = ok({"status": status, "output": [],
                                                           "incomplete_details": {"reason": "content_filter"}})
                with self.assertRaises(ProviderError) as caught:
                    completion(profile, "", [{"role": "user", "content": "hi"}], [], 1000)
                self.assertTrue(caught.exception.retryable)
                self.assertTrue(caught.exception.diagnostic["incomplete_response"])
        # The token limit stays a stop, also in the Responses format.
        with patch("swarm_bench.providers.build_opener") as opener:
            opener.return_value.open.return_value = ok({"status": "incomplete", "output": [],
                                                       "incomplete_details": {"reason": "max_output_tokens"}})
            with self.assertRaises(ProviderError) as caught:
                completion(profile, "", [{"role": "user", "content": "hi"}], [], 1000)
            self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
