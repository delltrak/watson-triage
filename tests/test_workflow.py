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
    """The cursor must not outlive a step that can still fail."""

    def test_a_validated_run_notifies_without_crashing(self):
        # notify() reads config for send_video, and only on the validation
        # branch -- so dropping that parameter raised NameError on exactly the
        # path no test drove, after the cursor had already been saved. The
        # update was then permanently suppressed.
        from watson.workflow import notify

        sent = []

        class FakePlow:
            def send(self, chat, body, media):
                sent.append((chat, body, media))
                return {'uid': 'receipt'}

        store = Store(self.home)
        self.addCleanup(store.db.close)
        validation = {'status': 'failed', 'video': '/tmp/none.mp4',
                      'steps': [{'status': 'failed', 'expected': 'ok', 'actual': 'boom'}]}
        result = {'summary': 'resumo'}
        receipt = notify(store, (FakePlow(), 'chat'), {'notify_owner': True},
                         1, ISSUE, result, 'reproduced', validation)
        self.assertIsNotNone(receipt)
        chat, body, media = sent[0]
        self.assertEqual(chat, 'chat')
        self.assertIn('Esperado: ok', body)
        self.assertIn('Observado: boom', body)
        # send_video is absent from config, so no media rides along.
        self.assertIsNone(media)

    def test_a_failed_comment_preflight_leaves_the_update_retryable(self):
        # Same checkpoint hazard as the chat lookup, one step further along:
        # the writer's fresh-issue read and the model's comment composition are
        # both fallible and both side-effect-free, so they must land before the
        # cursor is saved. After it, a failure marks the issue handled with
        # nothing posted and the next cycle never looks again.
        writer = Mock()
        writer.prepare.side_effect = WatsonError('a atribuição mudou')

        cfg = dict(self.cfg, github_comments=True, notify_owner=False)
        private_json(self.home / 'config.json', cfg)
        outcome = cycle(self.home, model=Model(), github=FakeGitHub(), writer=writer)

        self.assertEqual(outcome['processed'], [])
        self.assertTrue(outcome['errors'])
        writer.send.assert_not_called()
        store = Store(self.home)
        self.addCleanup(store.db.close)
        self.assertIsNone(Cases(store).get('demo/repo', 7),
                          'cursor was saved despite the failed preflight — the update is now lost')

    def test_a_failed_comment_still_lets_the_owner_notification_run(self):
        # Past the checkpoint the two sends are independent: a GitHub comment
        # is optional, the owner's update is the product. Letting the comment's
        # failure skip notify() lost that update for good, since the cursor was
        # already saved.
        sent = []

        class FakePlow:
            def send(self, chat, body, media):
                sent.append(body); return {'uid': 'receipt'}
            def owner_chat(self): return 'chat'

        writer = Mock()
        writer.prepare.return_value = {'existing': None, 'number': 7}
        writer.send.side_effect = WatsonError('a issue fechou entre a leitura e o post')

        cfg = dict(self.cfg, github_comments=True, notify_owner=True)
        private_json(self.home / 'config.json', cfg)
        with patch('watson.workflow.Plow') as plow:
            plow.from_config.return_value = FakePlow()
            outcome = cycle(self.home, model=Model(), github=FakeGitHub(), writer=writer)

        self.assertTrue(sent, 'the comment failure suppressed the owner notification')
        self.assertTrue(outcome['errors'], 'the comment failure was swallowed')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.gh=FakeGitHub(); self.model=Model()
        self.cfg={**CONFIG,'assignee':'owner','github_comments':True,'notify_owner':False,
                  'browser_profiles':{'7':{'url':'http://127.0.0.1:1234','test_environment':True,'source_sha':'a'*40,
                  'login':{'username':'#email','password':'#password','submit':'button','ready':'#ready'},
                  'steps':[{'action':'expect_text','selector':'#result','value':'Required'}]}}}
        private_json(self.home/'config.json',self.cfg)
        s=Store(self.home); s.track('demo/repo',7); s.db.close()
        self.writer=Mock()
        self.writer.prepare.return_value={'existing':None,'number':7}
        self.writer.send.return_value={'url':'https://github.com/demo/repo/issues/7#comment'}

    def test_wait_restart_reply_resume_and_no_duplicate(self):
        first=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(first['processed'][0]['state'],'waiting_access')
        unchanged=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(unchanged['unchanged'],[7]); self.assertEqual(self.writer.send.call_count,1)
        save_access(self.home,7,'fake','not-real')
        self.gh.item['comments'].append({'id':12,'body':'Access sent privately. Please try again.','author':'author','url':ISSUE['url']})
        validation={'status':'failed','steps':[{'status':'failed','expected':'Required','actual':'Created'}],'video':None}
        with patch('watson.workflow.run_browser',return_value=validation) as browser:
            resumed=cycle(self.home,model=self.model,github=self.gh,writer=self.writer)
        self.assertEqual(resumed['processed'][0]['state'],'reproduced'); browser.assert_called_once()
        s=Store(self.home); case=Cases(s).get('demo/repo',7); s.db.close()
        self.assertEqual(case['data']['previous_state'],'waiting_access')
        self.assertEqual(self.writer.send.call_count,2)

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
