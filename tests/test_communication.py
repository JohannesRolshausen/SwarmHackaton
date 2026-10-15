import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_bench.common import ROOT
from swarm_bench.lab_engine import LabRun, DEFAULT_COMMUNICATION_PROMPT
from swarm_bench.lab_server import LabManager


def reply(*tools, text=''):
    return {'role':'assistant','content':text, 'tool_calls':[
        {'id':f'call_{i}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        for i,(name,args) in enumerate(tools)
    ]}, {'input_tokens':20,'output_tokens':5}


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manager=LabManager(ROOT/'data',self.root)
        self.manager.registry.save({'id':'fake','name':'Fake','kind':'openai_compatible',
                                    'base_url':'http://127.0.0.1:9/v1','model':'fake'})

    def create(self,n=5,**overrides):
        c={'scenario':'communication','custom_question':'What is 21 + 21?','agent_count':n,
           'mode':'live','models':{f'agent_{i:02d}':'fake' for i in range(1,n+1)},'call_limit':3,'retain_after_submit':False,**overrides}
        state=self.manager.create(c)
        return self.manager.runs[state['id']]

    def test_identical_exact_prompts_and_three_neutral_tools(self):
        r=self.create()
        self.assertEqual({r.prompt(a) for a in r.agents},{DEFAULT_COMMUNICATION_PROMPT})
        self.assertEqual({r.histories[a][1]['content'] for a in r.agents},{'What is 21 + 21?'})
        self.assertEqual(r.workspaces,{a:{} for a in r.agents})
        for a in r.agents:
            names=[t['function']['name'] for t in r.tools_for(a)]
            self.assertEqual(names,['read_board','post_note','submit_answer'])
            self.assertNotIn('team',json.dumps(r.tools_for(a)))
            self.assertNotIn('empty',json.dumps(r.tools_for(a)))
            self.assertNotIn(a,r.prompt(a))
            with self.assertRaises(ValueError):r.action(a,'read_file',{'path':'notes.json'})

    def test_five_agents_stop_after_terminal_tool_bundle_without_notifications(self):
        r=self.create();contexts=[]
        def model(_profile,_key,messages,*_):
            contexts.append(copy.deepcopy(messages))
            return reply(('post_note',{'content':'My answer is 42.'}),('submit_answer',{'answer':'42'}))
        with patch('swarm_bench.lab_engine.completion',model):r.run_free()
        self.assertEqual(len(contexts),5)
        self.assertTrue(all(len(h)==2 for h in contexts))
        self.assertEqual(r.state['status'],'complete')
        self.assertEqual(r.state['finish_reason'],'agents_finished')
        self.assertEqual(set(r.state['agent_status'].values()),{'done'})
        self.assertEqual(len(r.state['notes']),5)
        self.assertIsNone(r.metrics()['team_answer'])
        self.assertFalse(any(e['kind']=='continuation_requested' for e in r.state['events']))
        self.assertTrue(all(u['calls']==1 for u in r.state['usage'].values()))

    def test_plain_final_response_is_private_and_never_reactivated(self):
        r=self.create(n=2)
        with patch('swarm_bench.lab_engine.completion',return_value=reply(text='42')):r.live_call('agent_01')
        before=copy.deepcopy(r.histories['agent_01'])
        r.action('agent_02','post_note',{'content':'Is anyone there?'})
        with patch('swarm_bench.lab_engine.completion',return_value=reply(('submit_answer',{'answer':'42'}))):r.run_free()
        self.assertEqual(r.histories['agent_01'],before)
        self.assertEqual(r.state['usage']['agent_01']['calls'],1)
        self.assertEqual([n['agent_id'] for n in r.state['notes']],['agent_02'])
        e=next(e for e in r.state['events'] if e['kind']=='agent_finished' and e['agent_id']=='agent_01')
        self.assertEqual(e['response_text'],'42')
        self.assertEqual(e['reason'],'no_tool_response')

    def test_reading_is_voluntary_and_tool_results_are_available_next_call(self):
        r=self.create(n=1);contexts=[]
        def model(_p,_k,messages,*_):
            contexts.append(copy.deepcopy(messages))
            return reply(('read_board',{})) if len(contexts)==1 else reply(('submit_answer',{'answer':'42'}))
        with patch('swarm_bench.lab_engine.completion',model):r.run_free()
        self.assertEqual(len(contexts),2)
        self.assertEqual(contexts[1][-1]['role'],'tool')
        self.assertEqual(json.loads(contexts[1][-1]['content']),{'notes':[]})
        self.assertEqual([m['role'] for m in contexts[1]].count('user'),1)
        self.assertEqual(r.state['notes'],[])

    def test_timeout_is_retried_by_default_without_touching_the_history(self):
        from swarm_bench.providers import ProviderError
        r=self.create(n=1);contexts=[]
        def model(_p,_k,messages,*_):
            contexts.append(copy.deepcopy(messages))
            if len(contexts)==1:
                raise ProviderError('Provider did not respond within 90 seconds.',retryable=True)
            return reply(('submit_answer',{'answer':'42'}))
        with patch('swarm_bench.lab_engine.completion',model),patch.object(r.pause_requested,'wait',return_value=False):
            r.run_free()
        self.assertEqual(r.config['transient_retries'],2)
        self.assertEqual(contexts[0],contexts[1])
        self.assertEqual(r.state['usage']['agent_01']['calls'],2)
        self.assertEqual(r.state['usage']['agent_01']['failed_calls'],1)
        self.assertEqual(r.state['agent_status']['agent_01'],'done')
        self.assertEqual(r.state['answers']['agent_01'][-1]['answer'],'42')
        self.assertTrue(any(e['kind']=='retry_scheduled' for e in r.state['events']))

    def test_cap_and_invalid_submission_do_not_fabricate_finished_answers(self):
        r=self.create(n=1)
        with self.assertRaises(ValueError):r.action('agent_01','submit_answer',{'answer':''})
        with self.assertRaises(ValueError):r.action('agent_01','submit_answer',{'answer':42})
        self.assertEqual(r.action('agent_01','read_board',{'unused':'argument'}), {'notes': []})
        with patch('swarm_bench.lab_engine.completion',return_value=reply(('read_board',{}))):r.run_free()
        self.assertEqual(r.state['usage']['agent_01']['calls'],3)
        self.assertEqual(r.state['agent_status']['agent_01'],'limit')
        self.assertEqual(r.state['finish_reason'],'call_limit')
        self.assertEqual(r.state['answers']['agent_01'],[])

    def test_incompatible_treatments_rejected_and_custom_prompt_preserved(self):
        for c in ({'restricted':['agent_01']},{'leader':'agent_02'},{'idle_policy':'continue'},
                  {'board_delivery':'auto'},{'answer_policy':'plurality'},
                  {'workspace_files':{'agent_01':{'x.txt':'x'}}},
                  {'agent_prompts':{'agent_01':'Collaborate'}},{'enabled_tools':['read_board']},
                  {'models':{'agent_01':'fake','agent_02':'different'}}):
            with self.subTest(c=c),self.assertRaises(ValueError):self.create(n=2,**c)
        r=self.create(n=2,common_prompt='Other agents have the same problem. Solve it.')
        self.assertEqual(r.prompt('agent_01'),'Other agents have the same problem. Solve it.')
        loaded=LabManager(ROOT/'data',self.root)
        self.assertEqual(loaded.inspect(r.id,'agent_01')['files'],{})


if __name__=='__main__':unittest.main()
