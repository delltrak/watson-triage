import copy
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from watson.analysis import RESULT_SCHEMA, triage, validate_result
from watson.cli import sync
from watson.core import Store, WatsonError, safe_source
from watson.delivery import Plow, deliver
from watson.github import GitHub


ISSUE = {'repository': 'demo/repo', 'number': 7, 'title': 'Falha no calendário', 'body': '',
         'state': 'open', 'state_reason': None, 'author': 'author', 'assignees': ['owner'],
         'url': 'https://github.com/demo/repo/issues/7', 'updated_at': '2026-09-15T00:00:00Z', 'comments': []}
CONFIG = {'repository': 'demo/repo', 'assignee': 'owner', 'related_repositories': []}
RESULT = {'status': 'needs_info', 'summary': 'Falta confirmar a versão.',
          'voice_script': 'O autor relatou uma falha. Precisamos confirmar a versão antes de investigar.',
          'findings': [{'claim': 'Falha relatada.', 'evidence_ids': ['issue'], 'certainty': 'reported'}],
          'next_steps': ['Confirmar versão.'], 'questions_for_author': ['Qual versão?'],
          'branch_recommendation': 'premature', 'limitations': []}


class FakeGitHub:
    def __init__(self):
        self.item = copy.deepcopy(ISSUE)
        self.sha = 'a' * 40
        self.items = [self.item]

    def issue(self, repo, number): return copy.deepcopy(self.item)
    def references(self, issue): return [], []
    def source_index(self, repo): return {'repository': repo, 'sha': self.sha, 'paths': ['src/calendar.ts']}
    def assigned(self, repo, assignee): return self.items
    def file(self, repo, path, sha):
        return {'repository': repo, 'path': path, 'sha': sha,
                'url': f'https://github.com/{repo}/blob/{sha}/{path}', 'content': '1: export const version = 1;'}


class FakeModel:
    def __init__(self): self.calls = 0
    def ask(self, instruction, payload, schema, label):
        self.calls += 1
        return copy.deepcopy(RESULT) if schema == RESULT_SCHEMA else {'paths': ['src/calendar.ts'], 'reason': 'Calendário'}


class WatsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.db.close)
        self.github, self.model = FakeGitHub(), FakeModel()

    def test_no_repeated_inference_after_restart(self):
        first = triage(self.store, self.github, self.model, CONFIG, 7)
        second_store = Store(self.home)
        self.addCleanup(second_store.db.close)
        second = triage(second_store, self.github, self.model, CONFIG, 7)
        self.assertTrue(second['cached'])
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(self.model.calls, 2)

    def test_comment_or_revision_invalidates_cache(self):
        triage(self.store, self.github, self.model, CONFIG, 7)
        self.github.item['comments'].append({'id': 99, 'body': 'Já corrigido', 'author': 'dev', 'url': ISSUE['url']})
        self.assertFalse(triage(self.store, self.github, self.model, CONFIG, 7)['cached'])
        self.github.sha = 'b' * 40
        self.assertFalse(triage(self.store, self.github, self.model, CONFIG, 7)['cached'])
        self.assertEqual(self.model.calls, 6)

    def test_untrusted_selected_path_rejected_and_retryable(self):
        class Attack:
            def ask(self, *args): return {'paths': ['../../auth.json']}
        with self.assertRaises(WatsonError):
            triage(self.store, self.github, Attack(), CONFIG, 7)
        self.assertEqual(self.store.run(1)['status'], 'failed')
        fixed = triage(self.store, self.github, self.model, CONFIG, 7)
        self.assertEqual(fixed['run_id'], 1)

    def test_fabricated_evidence_id_rejected(self):
        result = copy.deepcopy(RESULT)
        result['findings'][0]['evidence_ids'] = ['production:verified']
        with self.assertRaises(WatsonError): validate_result(result, {'issue': ISSUE})

    def test_initial_backlog_is_not_automatically_triaged(self):
        self.assertTrue(sync(self.store, self.github, CONFIG)['initial_baseline'])
        self.assertEqual(self.store.history()['issues'][0]['tracked'], 0)
        new = {**copy.deepcopy(ISSUE), 'number': 8}
        self.github.items.append(new)
        self.assertEqual(sync(self.store, self.github, CONFIG)['newly_tracked'], [8])
        self.assertEqual(sync(self.store, self.github, CONFIG)['newly_tracked'], [])

    def test_all_github_calls_are_get_and_allowlisted(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '[]', '')
        github = GitHub(['demo/repo'], run=run)
        github.get('demo/repo', 'commits?per_page=1')
        with self.assertRaises(WatsonError): github.get('other/repo', 'issues/1')
        with self.assertRaises(WatsonError): github.get('demo/repo', 'merges')
        with self.assertRaises(WatsonError): github.get('demo/repo', 'issues/../../merges')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2:4], ['--method', 'GET'])
        self.assertFalse(hasattr(github, 'merge'))

    def test_reassignment_tracks_an_existing_issue(self):
        sync(self.store, self.github, CONFIG)
        self.github.items = []
        sync(self.store, self.github, CONFIG)
        self.github.items = [copy.deepcopy(ISSUE)]
        self.assertEqual(sync(self.store, self.github, CONFIG)['newly_tracked'], [7])

    def test_init_records_whether_the_owner_hears_about_updates(self):
        # `notify()` returns immediately when notify_owner is absent, so an
        # init that never wrote the key promised unattended updates and then
        # silently dropped every one of them.
        from watson.cli import main
        for flag, expected in ((['--notify-owner'], True), ([], False)):
            with tempfile.TemporaryDirectory() as folder:
                self.assertEqual(main(['--home', folder, 'init', '--repo', 'demo/repo',
                                       '--assignee', 'owner'] + flag), 0)
                config = json.loads((Path(folder) / 'config.json').read_text())
                self.assertEqual(config['notify_owner'], expected)

    def test_secret_and_traversal_paths_excluded(self):
        for path in ['../a.py', '/etc/config.json', '.env', 'app/.env.json', 'auth.json', 'keys/private-key.json']:
            self.assertFalse(safe_source(path), path)
        self.assertTrue(safe_source('src/calendar.tsx'))

    def test_unknown_delivery_is_never_replayed(self):
        run = triage(self.store, self.github, self.model, CONFIG, 7)
        class TimeoutPlow:
            calls = 0
            def owner_chat(self): return 'owner-chat'
            def send(self, *args):
                self.calls += 1
                raise TimeoutError()
        plow = TimeoutPlow()
        with self.assertRaises(WatsonError): deliver(self.store, run['run_id'], plow)
        with self.assertRaises(WatsonError): deliver(self.store, run['run_id'], plow)
        self.assertEqual(plow.calls, 1)
        self.assertEqual(self.store.history()['actions'][0]['status'], 'unknown')

    def test_plow_owner_identity_and_upload_contract(self):
        calls = []
        identity = {'line': {'uid': 'line'}, 'chats': [{'uid': 'chat', 'status': 'active', 'participants': [
            {'type': 'agent', 'relationship': 'self', 'line': {'uid': 'line'}},
            {'type': 'member', 'role': 'owner'}]}]}
        def request(method, url, data=None, headers=None):
            calls.append((method, url, data, headers))
            if url.endswith('/me'): return identity
            if url.endswith('/attachments'):
                return {'uid': 'audio-id', 'upload_url': 'https://upload.example/file', 'upload_headers': {'x-upload': 'capability'}}
            if method == 'PUT': return {}
            return {'uid': 'message'}
        plow = Plow(token='test-token', request=request)
        self.assertEqual(plow.owner_chat(), 'chat')
        audio = self.home / 'test.m4a'
        audio.write_bytes(b'audio')
        self.assertEqual(plow.send('chat', 'Hello', audio)['message_uid'], 'message')
        put = next(c for c in calls if c[0] == 'PUT')
        self.assertEqual(put[3], {'x-upload': 'capability'})
        self.assertNotIn('test-token', str(put))
        self.assertEqual(json.loads(calls[-1][2])['attachment_uids'], ['audio-id'])
        declaration = next(c for c in calls if c[1].endswith('/attachments'))
        self.assertEqual(json.loads(declaration[2])['content_type'], 'audio/mp4')
        identity['chats'][0]['participants'].append({'type': 'member', 'role': 'member'})
        with self.assertRaises(WatsonError): plow.owner_chat()

    def test_accepted_delivery_receipt_and_deduplication(self):
        run = triage(self.store, self.github, self.model, CONFIG, 7)
        class GoodPlow:
            def owner_chat(self): return 'chat'
            def send(self, *args): return {'message_uid': 'msg', 'chat_uid': 'chat'}
        deliver(self.store, run['run_id'], GoodPlow())
        self.assertEqual(self.store.history()['actions'][0]['status'], 'accepted')
        with self.assertRaises(WatsonError): deliver(self.store, run['run_id'], GoodPlow())

    def test_transient_get_retries_but_post_never_retries(self):
        calls = []
        def unavailable(method, url, *args):
            calls.append(method)
            raise urllib.error.HTTPError(url, 503, 'Unavailable', {}, None)
        plow = Plow(token='test', request=unavailable)
        with patch('watson.delivery.time.sleep'):
            with self.assertRaises(WatsonError): plow.owner_chat()
            self.assertEqual(calls, ['GET', 'GET', 'GET'])
            with self.assertRaises(WatsonError): plow.send('chat', 'Test')
            self.assertEqual(calls, ['GET', 'GET', 'GET', 'POST'])

    def test_credential_file_is_parsed_without_executing_code(self):
        path = self.home / 'plow-credentials'
        path.write_text('PLOW_API_BASE="https://api.plow.co"\nPLOW_AGENT_TOKEN="test-token"\n# agent metadata\n')
        self.assertEqual(Plow.from_config({'plow_credential_file': str(path)}).token, 'test-token')
        path.write_text('PLOW_API_BASE=https://attacker.example\nPLOW_AGENT_TOKEN=test-token\n')
        with self.assertRaises(WatsonError): Plow.from_config({'plow_credential_file': str(path)})


if __name__ == '__main__':
    unittest.main()
