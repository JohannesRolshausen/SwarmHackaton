"""A fixed provider behind OpenRouter: the request, the profile checks, the provider that served the call and the export.
Fake providers only, no paid calls."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_bench.common import ROOT
from swarm_bench.error_help import explain_failure
from swarm_bench.lab_server import LabManager
from swarm_bench.providers import ProviderRegistry, completion

from test_rounds_and_flex import USAGE, Reply, RunHelper, rounds_design

PROFILE = {"kind": "openai_compatible", "model": "xiaomi/mimo-v2.6-pro", "reasoning_effort": "high",
           "base_url": "https://openrouter.ai/api/v1", "openrouter_provider": "deepinfra/fp8"}
ANSWER = {"provider": "DeepInfra", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 3}}


def send(profile, reply=ANSWER):
    with patch("swarm_bench.providers.build_opener") as opener:
        opener.return_value.open.return_value = Reply(reply)
        result = completion(profile, "sk-test", [{"role": "user", "content": "x"}], [], 100)
    return json.loads(opener.return_value.open.call_args.args[0].data), result


class RequestTests(unittest.TestCase):
    def test_fixed_provider_is_sent_without_fallbacks_and_the_served_provider_is_kept(self):
        body, (_, usage) = send(PROFILE)
        self.assertEqual(body["provider"], {"order": ["deepinfra/fp8"], "allow_fallbacks": False})
        self.assertEqual(body["reasoning"]["effort"], "high")
        self.assertEqual(usage["served_provider"], "DeepInfra")
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (10, 3))

    def test_without_the_field_openrouter_chooses_and_the_served_provider_is_still_kept(self):
        body, (_, usage) = send({**PROFILE, "openrouter_provider": ""})
        self.assertNotIn("provider", body)
        self.assertEqual(usage["served_provider"], "DeepInfra")

    def test_a_price_limit_of_the_model_stays_next_to_the_fixed_provider(self):
        body, _ = send({**PROFILE, "model": "nvidia/nemotron-3-ultra-550b-a55b"})
        self.assertEqual(set(body["provider"]), {"max_price", "order", "allow_fallbacks"})

    def test_other_servers_get_no_routing_and_their_provider_field_is_ignored(self):
        body, (_, usage) = send({**PROFILE, "base_url": "https://api.example.com/v1"})
        self.assertNotIn("provider", body)
        self.assertNotIn("served_provider", usage)

    def test_an_odd_provider_name_is_not_stored(self):
        for odd in ("x" * 200, "<script>", 5, None):
            with self.subTest(odd):
                _, (_, usage) = send(PROFILE, {**ANSWER, "provider": odd})
                self.assertNotIn("served_provider", usage)


class ProfileTests(unittest.TestCase):
    base = {"id": "mimo", "name": "MiMo", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
            "model": "xiaomi/mimo-v2.6-pro"}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.registry = ProviderRegistry(Path(folder.name) / "models.json")

    def test_slug_is_saved_with_or_without_a_variant(self):
        for slug in ("deepinfra", "deepinfra/fp8", "gmicloud/bf16"):
            saved = self.registry.save({**self.base, "openrouter_provider": slug})
            self.assertEqual(saved[-1]["openrouter_provider"], slug)

    def test_invalid_slugs_and_other_servers_are_rejected(self):
        for slug in ("DeepInfra", "a/b/c", "deep infra", "/fp8", "deepinfra/"):
            with self.subTest(slug), self.assertRaisesRegex(ValueError, "Invalid OpenRouter provider"):
                self.registry.save({**self.base, "openrouter_provider": slug})
        for change in ({"base_url": "https://api.example.com/v1"}, {"kind": "anthropic"}):
            with self.subTest(change), self.assertRaisesRegex(ValueError, "only works with OpenRouter"):
                self.registry.save({**self.base, **change, "openrouter_provider": "deepinfra"})


class HelpTests(unittest.TestCase):
    def test_a_404_names_the_fixed_provider(self):
        title, what, _ = explain_failure("Provider returned HTTP 404.", {"http_status": 404},
                                         {**PROFILE, "name": "MiMo"}, run_ends=True, resumable=False)
        self.assertIn("deepinfra/fp8", title)
        self.assertIn("no other provider", what)
        title, _, _ = explain_failure("Provider returned HTTP 404.", {"http_status": 404},
                                      {**PROFILE, "name": "MiMo", "openrouter_provider": ""}, run_ends=True, resumable=False)
        self.assertIn("Model or address not found", title)


class ExportTests(RunHelper):
    def test_export_counts_the_served_providers_per_side(self):
        manager = LabManager(ROOT / "data", self.root / "lab", designs_dir=self.root / "designs")
        models = {"agent_01": "decider", "agent_02": "persuader", "agent_03": "persuader", "agent_04": "persuader"}
        created = manager.create({"scenario": "designed", "design": rounds_design(), "mode": "live", "models": models})
        run = manager.runs[created["id"]]
        for agent, name in (("agent_01", "OpenAI"), ("agent_02", "DeepInfra"), ("agent_02", "DeepInfra"), ("agent_03", "Xiaomi")):
            run.record_usage(agent, {**USAGE, "served_provider": name})
        row = next(csv.DictReader(io.StringIO(manager.export_csv())))
        self.assertEqual(row["leader_served_providers"], "OpenAI:1")
        self.assertEqual(row["others_served_providers"], "DeepInfra:2 Xiaomi:1")


if __name__ == "__main__":
    unittest.main()
