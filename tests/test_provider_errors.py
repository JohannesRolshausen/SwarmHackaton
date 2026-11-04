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

    def test_an_empty_reply_without_output_tokens_is_a_wait(self):
        # The reply Gemini sent in run 20261104-123705-db91dd.
        with self.assertRaises(ProviderError) as caught:
            self.call({'choices':[{'finish_reason':'stop','index':0,'message':{'role':'assistant'}}],
                'usage':{'completion_tokens':0,'prompt_tokens':5398,'total_tokens':5398}})
        self.assertTrue(caught.exception.diagnostic['empty_reply'])
        self.assertTrue(caught.exception.diagnostic['incomplete_response'])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.usage['input_tokens'],5398)

    def test_an_answer_with_anything_in_it_is_not_an_empty_reply(self):
        calls = [{'id':'c','function':{'name':'read_board','arguments':'{}'}}]
        for message, tokens in (({'content':'Fine.'},0), ({'content':'','tool_calls':calls},0),
                                ({'content':'','reasoning_content':'Let me think.'},0), ({'content':''},12)):
            msg, usage = self.call({'choices':[{'message':message}],
                'usage':{'prompt_tokens':10,'completion_tokens':tokens}})
            self.assertEqual(msg['role'],'assistant')
            self.assertEqual(usage['output_tokens'],tokens)

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
