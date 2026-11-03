"""The error banner: every error of a run gets an explanation next to its original message."""
import unittest
from datetime import datetime, timedelta

from swarm_bench.error_help import explain_failure, package_help, provider_label, request_help, run_issues
from swarm_bench.lab_engine import _wait_reason
from swarm_bench.providers import ProviderError

from test_rounds_and_flex import Recorder, RunHelper, rounds_design

GOOGLE = {"name": "Gemini 3", "kind": "openai_compatible", "model": "gemini-3-pro",
          "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"}
AT = "2026-11-03T10:00:00+00:00"


def state(events, status="running", agent_status=None, finish_reason=None, agents=("agent_01", "agent_02")):
    return {"status": status, "finish_reason": finish_reason, "events": [{"kind": "started", "at": AT}] + events,
            "config": {"agents": list(agents)}, "provider_profiles": {a: GOOGLE for a in agents},
            "agent_status": agent_status or {a: "working" for a in agents}}


def wait(agent, reason, diagnostic, delay=60, at=AT, message="Provider returned HTTP 429. Check the profile and limits."):
    return [{"kind": "model_error", "agent_id": agent, "at": at, "message": message, "diagnostic": diagnostic,
             "provider": GOOGLE},
            {"kind": "retry_scheduled", "agent_id": agent, "at": at, "reason": reason, "delay_seconds": delay}]


class ExplanationTests(unittest.TestCase):
    def test_provider_names_come_from_the_address(self):
        self.assertEqual(provider_label(GOOGLE), "Google")
        self.assertEqual(provider_label({"base_url": "https://openrouter.ai/api/v1"}), "OpenRouter")
        self.assertEqual(provider_label({"kind": "anthropic", "base_url": "https://proxy.example/v1"}), "Anthropic")
        self.assertEqual(provider_label({"base_url": "http://127.0.0.1:8000/v1"}), "the local model server")

    def test_a_rate_limit_says_the_run_goes_on_and_when(self):
        issues = run_issues(state(wait("agent_01", "rate_limit", {"http_status": 429}),
                                  agent_status={"agent_01": "rate_limited", "agent_02": "waiting"}))
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["title"], "Rate limit at Google")
        self.assertTrue(issue["continues"])
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["original"], "Provider returned HTTP 429. Check the profile and limits.")
        self.assertEqual(datetime.fromisoformat(issue["resume_at"]) - datetime.fromisoformat(AT), timedelta(seconds=60))

    def test_a_wait_is_shown_only_while_the_agent_waits(self):
        events = wait("agent_01", "rate_limit", {"http_status": 429})
        self.assertEqual(run_issues(state(events)), [])
        self.assertEqual(run_issues(state(events, status="paused",
                                          agent_status={"agent_01": "rate_limited", "agent_02": "ready"})), [])

    def test_agents_waiting_on_the_same_cause_share_one_line(self):
        later = (datetime.fromisoformat(AT) + timedelta(seconds=5)).isoformat()
        events = wait("agent_01", "rate_limit", {"http_status": 429}) + wait("agent_02", "rate_limit",
                                                                             {"http_status": 429}, at=later)
        issues = run_issues(state(events, agent_status={"agent_01": "rate_limited", "agent_02": "rate_limited"}))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["agents"], ["agent_01", "agent_02"])
        self.assertEqual(issues[0]["resume_at"], (datetime.fromisoformat(AT) + timedelta(seconds=60)).isoformat())

    def test_missing_credit_names_the_deadline_from_the_first_refusal(self):
        refused = {"http_status": 429, "action_required": True, "provider_error_code_name": "insufficient_quota"}
        later = (datetime.fromisoformat(AT) + timedelta(minutes=3)).isoformat()
        events = wait("agent_01", "action_required", refused) + wait("agent_01", "action_required", refused, at=later)
        issue = run_issues(state(events, agent_status={"agent_01": "rate_limited", "agent_02": "waiting"}))[0]
        self.assertEqual(issue["severity"], "error")
        self.assertIn("insufficient_quota", issue["what"])
        self.assertIn("Add credit", issue["todo"])
        self.assertEqual(datetime.fromisoformat(issue["gives_up_at"]) - datetime.fromisoformat(AT), timedelta(minutes=30))

    def test_a_used_up_token_budget_is_red(self):
        run = state([], status="stopped", finish_reason="token_limit")
        run["token_budget"] = {"accounted": 99900, "limit": 100000, "in_flight": 0}
        issue = run_issues(run)[0]
        self.assertEqual((issue["severity"], issue["title"], issue["continues"]), ("error", "Token budget used up", False))
        self.assertIn("99,900 of 100,000", issue["what"])

    def test_each_wait_reason_has_its_own_explanation(self):
        cases = {"timeout": ({"timeout_seconds": 380}, "did not answer within 380 seconds"),
                 "network": ({"network_error": True}, "connection to Google failed"),
                 "server_error": ({"http_status": 503}, "HTTP 503 (service unavailable or overloaded)"),
                 "flex_unavailable": ({"http_status": 429, "flex_unavailable": True}, "no Flex capacity")}
        for reason, (diagnostic, text) in cases.items():
            issue = run_issues(state(wait("agent_01", reason, diagnostic),
                                     agent_status={"agent_01": "rate_limited", "agent_02": "waiting"}))[0]
            self.assertIn(text, issue["what"], reason)

    def test_anthropic_overloaded_is_a_wait(self):
        self.assertEqual(_wait_reason(ProviderError("HTTP 529", diagnostic={"http_status": 529})), "server_error")

    def test_a_wrong_key_says_what_to_check(self):
        title, what, todo = explain_failure("Provider returned HTTP 401.", {"http_status": 401}, GOOGLE,
                                            run_ends=False, resumable=True)
        self.assertEqual(title, "Google rejected the API key")
        self.assertIn("Gemini 3", todo)
        self.assertIn("Resume", todo)

    def test_an_error_from_an_older_run_takes_the_diagnostic_of_the_failed_call(self):
        message = "Provider returned HTTP 404. Check the profile and limits."
        events = [{"kind": "model_error", "agent_id": "agent_02", "at": AT, "message": message,
                   "diagnostic": {"http_status": 404}},
                  {"kind": "error", "agent_id": "agent_02", "at": AT, "message": message}]
        issue = run_issues(state(events, status="complete"))[0]
        self.assertEqual(issue["title"], "Model or address not found at Google")
        self.assertFalse(issue["continues"])
        self.assertEqual(issue["original"], message)

    def test_an_error_before_a_resume_is_no_longer_shown(self):
        events = [{"kind": "error", "agent_id": "agent_02", "at": AT, "message": "Provider returned HTTP 400.",
                   "diagnostic": {"http_status": 400}}, {"kind": "started", "at": AT}]
        self.assertEqual(run_issues(state(events)), [])

    def test_hints_for_requests_and_packages(self):
        self.assertIn("Models", request_help("No key for Gemini 3. No call sent."))
        self.assertIsNone(request_help("Invalid temperature"))
        self.assertIn("Continue package", package_help("Run without any successful model call: boom"))


class EngineTests(RunHelper):
    def test_an_agent_that_fails_for_good_is_explained_with_the_run_ending(self):
        broken = ProviderError("Provider returned HTTP 400. Check the profile and limits.", diagnostic={"http_status": 400})
        run = self.run_rounds(rounds_design(after_rounds=2, stop_on_agent_error=True),
                              Recorder(fail={("seller-2", 1): broken}),
                              profiles=None)
        error = next(e for e in run.state["events"] if e["kind"] == "error")
        self.assertEqual(error["diagnostic"], {"http_status": 400})
        issues = run_issues(run.snapshot())
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["agents"], ["agent_03"])
        self.assertIn("rejected the request", issue["title"])
        self.assertIn("design ends the run", issue["consequence"])
        self.assertIn("does not belong in the evaluation", issue["todo"])
        self.assertEqual(issue["original"], "Provider returned HTTP 400. Check the profile and limits.")


if __name__ == "__main__":
    unittest.main()
