import unittest
from unittest.mock import patch
from swarm_bench.providers import ProviderError, completion
import json


class Reply:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, _): return json.dumps(self.data).encode()


class ProviderErrorTests(unittest.TestCase):
    def call(self, data):
        profile = {'kind':'openai_compatible', 'model':'test', 'base_url':'https://openrouter.ai/api/v1'}
        with patch('swarm_bench.providers.build_opener') as opener:
            opener.return_value.open.return_value = Reply(data)
            return completion(profile, 'secret-test', [], [], 8192)

    def test_http_200_error_preserves_code_without_sensitive_body(self):
        with self.assertRaises(ProviderError) as caught:
            self.call({'error':{'code':503,'message':'secret-test private content'}})
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.diagnostic['provider_error_code'],503)
        self.assertNotIn('secret-test',str(caught.exception))

    def test_null_message_is_a_wait_not_an_invented_answer(self):
        with self.assertRaises(ProviderError) as caught:
            self.call({'choices':[{'message':None}]})
        self.assertEqual(caught.exception.diagnostic['message_type'],'NoneType')
        self.assertTrue(caught.exception.diagnostic['incomplete_response'])
        self.assertTrue(caught.exception.retryable)

    def test_null_tools_and_reasoning_usage(self):
        msg, usage = self.call({'choices':[{'message':{'content':'42','tool_calls':None}}],
            'usage':{'prompt_tokens':10,'completion_tokens':20,'completion_tokens_details':{'reasoning_tokens':15},'cost':0.001}})
        self.assertEqual(msg['content'],'42')
        self.assertEqual(usage['reasoning_tokens'],15)
        self.assertEqual(usage['cost_usd'],0.001)

    def test_truncated_tool_not_executed(self):
        with self.assertRaises(ProviderError) as caught:
            self.call({'choices':[{'finish_reason':'length','message':{'tool_calls':[]}}]})
        self.assertFalse(caught.exception.retryable)
