"""Claude on Amazon Bedrock: the Anthropic Messages format against the bedrock-mantle endpoint. No real call."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_bench.common import ROOT
from swarm_bench.error_help import provider_label
from swarm_bench.providers import ProviderRegistry, completion

URL = "https://bedrock-mantle.eu-central-1.api.aws/anthropic/v1"
PROFILE = {"id": "opus-bedrock", "name": "Opus 5.5 · Bedrock", "kind": "anthropic", "base_url": URL,
           "model": "anthropic.claude-opus-5-5", "key_env": "AWS_BEARER_TOKEN_BEDROCK", "token_parameter": "max_tokens",
           "reasoning_effort": "high", "service_tier": "", "openrouter_provider": ""}
TOOL = {"type": "function", "function": {"name": "post_note", "description": "Post.",
                                         "parameters": {"type": "object", "properties": {}}}}


class Response:
    status, reason, headers = 200, "OK", {}

    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *args):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return Response({"id": "msg_1", "type": "message", "role": "assistant", "model": "anthropic.claude-opus-5-5",
                         "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 3},
                         "content": [{"type": "thinking", "thinking": "Is this a test?", "signature": "sig"},
                                     {"type": "text", "text": "Hello"}]})


class BedrockProfileTests(unittest.TestCase):
    def test_the_registry_saves_the_profile_and_reads_the_key_from_the_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = ProviderRegistry(Path(temp) / "profiles.json")
            registry.save(dict(PROFILE))
            with patch.dict("os.environ", {"AWS_BEARER_TOKEN_BEDROCK": "bedrock-key"}):
                profile, key = registry.resolve("opus-bedrock")
        self.assertEqual((profile["base_url"], profile["model"], key), (URL, "anthropic.claude-opus-5-5", "bedrock-key"))

    def test_the_call_goes_to_the_messages_endpoint_with_the_key_and_asks_for_the_thinking_summary(self):
        opener = Opener()
        with patch("swarm_bench.providers.build_opener", return_value=opener):
            message, usage = completion(PROFILE, "bedrock-key", [{"role": "system", "content": "You are Lena."},
                                                                 {"role": "user", "content": "Hi"}], [TOOL], 2000)
        request = opener.requests[0]
        body = json.loads(request.data)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(request.full_url, URL + "/messages")
        self.assertEqual(headers["x-api-key"], "bedrock-key")
        self.assertNotIn("authorization", headers)
        self.assertEqual(body["model"], "anthropic.claude-opus-5-5")
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual((message["reasoning_text"], message["content"]), ("Is this a test?", "Hello"))

    def test_errors_name_amazon_bedrock(self):
        self.assertEqual(provider_label(PROFILE), "Amazon Bedrock")

    def test_the_models_page_has_the_preset(self):
        script = (ROOT / "swarm_bench/web/app.js").read_text(encoding="utf-8")
        preset = re.search(r'\{group:"Amazon Bedrock".*?\}', script).group(0)
        for value in (URL, PROFILE["model"], PROFILE["key_env"], 'kind:"anthropic"', 'reasoning_effort:"high"'):
            self.assertIn(value, preset)


if __name__ == "__main__":
    unittest.main()
