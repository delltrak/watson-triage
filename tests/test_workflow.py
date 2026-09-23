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

class OwnerChannelTests(unittest.TestCase):
    """The cursor must not outlive a failed notify -- see owner_channel()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cfg = {**CONFIG, 'assignee': 'owner', 'notify_owner': True}
        private_json(self.home / 'config.json', self.cfg)
        store = Store(self.home); store.track('demo/repo', 7); store.db.close()

    def test_a_chat_lookup_failure_leaves_the_update_retryable(self):
        # The real bug was an ORDERING one: the cursor was persisted before
        # notify() resolved the chat, so one transient lookup failure made the
        # next cycle read the issue as unchanged and drop that update forever.
        # Asserting on owner_channel() alone would pass with the bug restored,
        # so this drives cycle() and looks at what it persisted.
        class Unreachable:
            @classmethod
            def from_config(cls, config):
                return cls()

            def owner_chat(self):
                raise WatsonError('sem chat')

        with patch('watson.workflow.Plow', Unreachable):
            outcome = cycle(self.home, model=FakeModel(), github=FakeGitHub(), writer=Mock())

        self.assertEqual(outcome['processed'], [])
        self.assertTrue(outcome['errors'])
        store = Store(self.home)
        self.addCleanup(store.db.close)
        self.assertIsNone(Cases(store).get('demo/repo', 7),
                          'cursor was saved despite the failed lookup — the update is now lost')

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

    def test_a_staged_claim_and_the_cursor_land_together_or_not_at_all(self):
        # The half-states are what this pins. A claim committed without its
        # cursor strands a key that every retry collides with -- the issue is
        # stuck for good. A cursor committed without its claim drops the update
        # silently. One transaction has neither.
        store = Store(self.home)
        self.addCleanup(store.db.close)
        cases = Cases(store)
        key = store.stage_action(1, 'github_comment', {'n': 7})

        staged = store.db.execute('SELECT COUNT(*) c FROM actions WHERE key=?', (key,)).fetchone()['c']
        self.assertEqual(staged, 1, 'staged row should be visible inside the open transaction')

        store.db.rollback()
        rolled = store.db.execute('SELECT COUNT(*) c FROM actions WHERE key=?', (key,)).fetchone()['c']
        self.assertEqual(rolled, 0, 'an uncommitted claim must not survive — that is the stuck-forever case')

        key = store.stage_action(1, 'github_comment', {'n': 7})
        cases.save('demo/repo', 7, 'cursor', 'waiting_info', {'ok': True})
        committed = store.db.execute('SELECT COUNT(*) c FROM actions WHERE key=?', (key,)).fetchone()['c']
        self.assertEqual(committed, 1)
        self.assertIsNotNone(cases.get('demo/repo', 7))

    def test_a_failed_issue_leaves_no_claim_for_the_next_one_to_publish(self):
        # TWO issues, and that is the whole point. With one, cycle()'s
        # `finally: store.db.close()` implicitly rolls back and the test passes
        # with no rollback at all -- which is exactly what the first version of
        # this test did. The hazard needs a SECOND commit to publish the first
        # issue's orphaned claim: SQLite does not auto-abort most statement
        # errors and the connection is shared across the loop.
        class TwoIssues(FakeGitHub):
            def __init__(self):
                super().__init__()
                second = copy.deepcopy(ISSUE); second['number'] = 8
                self.items = [self.item, second]

            def issue(self, repo, number):
                return copy.deepcopy(next(i for i in self.items if i['number'] == number))

        orphan_key = {}

        def stage_then_fail(store, github, run_id, issue, text, kind):
            key = store.stage_action(run_id, 'github_comment', {'n': issue['number']})
            if issue['number'] == 7:
                orphan_key['key'] = key
                raise WatsonError('falhou depois de reservar')
            return {'existing': None, 'number': issue['number'], 'key': key}

        writer = Mock()
        writer.prepare.side_effect = stage_then_fail
        writer.send.return_value = {'url': 'https://example.invalid/c'}

        private_json(self.home / 'config.json',
                     dict(self.cfg, github_comments=True, notify_owner=False, cycle_limit=2))
        store = Store(self.home)
        store.track('demo/repo', 7); store.track('demo/repo', 8)
        store.db.close()

        outcome = cycle(self.home, model=Model(), github=TwoIssues(), writer=writer)
        self.assertTrue(outcome['errors'], 'issue 7 was supposed to fail')
        self.assertTrue(outcome['processed'], 'issue 8 was supposed to commit after it')

        store = Store(self.home)
        self.addCleanup(store.db.close)
        # Issue 8's own claim is legitimately present; only issue 7's must not be.
        orphans = store.db.execute('SELECT COUNT(*) c FROM actions WHERE key=?',
                                   (orphan_key['key'],)).fetchone()['c']
        self.assertEqual(orphans, 0,
                         "issue 7's staged claim survived and issue 8's commit published it")

    def test_a_quiet_agent_resolves_no_channel(self):
        from watson.workflow import owner_channel
        self.assertIsNone(owner_channel({'notify_owner': False}))


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
        self.writer=Mock()
        self.writer.prepare.return_value={'existing':None,'number':7,'key':1}
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
