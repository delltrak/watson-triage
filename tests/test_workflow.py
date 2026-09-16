import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from watson.core import Store, private_json, WatsonError
from watson.cases import Cases
from watson.workflow import cycle, event_cursor
from watson.writes import GitHubWriter, MARKER
from watson.browser import validate_profile, save_access
from watson.metrics import record_usage
from test_watson import FakeGitHub, FakeModel, CONFIG, ISSUE

class Model(FakeModel):
    def ask(self, instruction, payload, schema, label):
        if label.startswith('comment-'):
            return {'language':'en','body':'Please provide a test account through the private channel. Do not post credentials here.'}
        return super().ask(instruction,payload,schema,label)

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.gh=FakeGitHub(); self.model=Model()
        self.cfg={**CONFIG,'assignee':'owner','github_comments':True,'notify_owner':False,
                  'browser_profiles':{'7':{'url':'http://127.0.0.1:1234','test_environment':True,'source_sha':'a'*40,
                  'login':{'username':'#email','password':'#password','submit':'button','ready':'#ready'},
                  'steps':[{'action':'expect_text','selector':'#result','value':'Required'}]}}}
        private_json(self.home/'config.json',self.cfg)
        s=Store(self.home); s.track('demo/repo',7); s.db.close()
        self.writer=Mock(); self.writer.comment.return_value={'url':'https://github.com/demo/repo/issues/7#comment'}

    def test_wait_restart_reply_resume_and_no_duplicate(self):
        first=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(first['processed'][0]['state'],'waiting_access')
        unchanged=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(unchanged['unchanged'],[7]); self.assertEqual(self.writer.comment.call_count,1)
        save_access(self.home,7,'fake','not-real')
        self.gh.item['comments'].append({'id':12,'body':'Access sent privately. Please try again.','author':'author','url':ISSUE['url']})
        validation={'status':'failed','steps':[{'status':'failed','expected':'Required','actual':'Created'}],'video':None}
        with patch('watson.workflow.run_browser',return_value=validation) as browser:
            resumed=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(resumed['processed'][0]['state'],'reproduced'); browser.assert_called_once()
        s=Store(self.home); case=Cases(s).get('demo/repo',7); s.db.close()
        self.assertEqual(case['data']['previous_state'],'waiting_access')
        self.assertEqual(self.writer.comment.call_count,2)

    def test_own_comment_does_not_trigger_cycle(self):
        first=event_cursor(copy.deepcopy(ISSUE),'a'*40,None,[])
        issue=copy.deepcopy(ISSUE); issue['updated_at']='different'
        issue['comments'].append({'body':MARKER+'marker -->','id':9})
        self.assertEqual(first,event_cursor(issue,'a'*40,None,[]))

    def test_no_merge_or_disabled_write(self):
        run=Mock(); writer=GitHubWriter('demo/repo',enabled=True,run=run)
        for resource in ['pulls/1/merge','merges','issues/1','git/refs/heads/main']:
            with self.assertRaises(WatsonError): writer.post(resource,{})
        with self.assertRaises(WatsonError): GitHubWriter('demo/repo',run=run).post('issues/7/comments',{})
        with self.assertRaises(WatsonError): writer.post('pulls',{'draft':False,'head':'watson/example'})
        with self.assertRaises(WatsonError): writer.post('git/refs',{'ref':'refs/heads/main'})
        run.assert_not_called()

    def test_missing_deployment_revision_blocks_browser(self):
        self.cfg['browser_profiles']['7'].pop('source_sha')
        private_json(self.home/'config.json',self.cfg)
        save_access(self.home,7,'fake','not-real')
        with patch('watson.workflow.run_browser') as browser:
            result=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(result['processed'][0]['state'],'blocked')
        browser.assert_not_called()

    def test_browser_requires_test_environment_and_assertion(self):
        for profile in [{'url':'https://example.com','steps':[]},
                        {'url':'http://example.com','test_environment':True,'steps':[{'action':'click','selector':'button'}]}]:
            with self.assertRaises(WatsonError): validate_profile(profile)

    def test_usage_records_attempts_without_double_counting_cache(self):
        import sqlite3
        for _ in range(2): record_usage(self.home,'same-label','actual-model',[{'input_tokens':100,'cached_input_tokens':30,'output_tokens':8}])
        conn=sqlite3.connect(self.home/'metrics/state.db')
        self.assertEqual(conn.execute('select sum(input_tokens),sum(cache_read_tokens),sum(output_tokens),count(*) from session_model_usage').fetchone(),(140,60,16,2))
        conn.close()
