import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watson.core import Store, WatsonError, private_json
from watson.issue_ref import parse_issue_ref, resolve_issue_number
from watson.mcp import dispatch, serve


class IssueRefTests(unittest.TestCase):
    def test_accepts_int_hash_and_github_url(self):
        self.assertEqual(parse_issue_ref(12), (None, 12))
        self.assertEqual(parse_issue_ref('12'), (None, 12))
        self.assertEqual(parse_issue_ref('#12'), (None, 12))
        self.assertEqual(
            parse_issue_ref('https://github.com/demo/repo/issues/123'),
            ('demo/repo', 123),
        )
        self.assertEqual(
            parse_issue_ref('https://www.github.com/Demo/Repo/issues/7/?x=1#c'),
            ('Demo/Repo', 7),
        )

    def test_rejects_noise_and_zero(self):
        for bad in [0, '0', '#0', True, '', '  ', 'not-an-issue',
                    'https://github.com/demo/repo/pull/1',
                    'https://evil.example/demo/repo/issues/1']:
            with self.assertRaises(WatsonError):
                parse_issue_ref(bad)

    def test_resolve_requires_configured_repository(self):
        config = {'repository': 'demo/repo'}
        self.assertEqual(resolve_issue_number('#9', config), 9)
        self.assertEqual(
            resolve_issue_number('https://github.com/demo/repo/issues/9', config), 9)
        with self.assertRaises(WatsonError) as ctx:
            resolve_issue_number('https://github.com/other/repo/issues/9', config)
        self.assertIn('demo/repo', str(ctx.exception))
        self.assertNotIn('PAT', str(ctx.exception))
        self.assertNotIn('branch', str(ctx.exception).lower())


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        store = Store(self.home)
        store.db.close()
        private_json(self.home / 'config.json', {
            'repository': 'demo/repo', 'assignee': 'owner', 'related_repositories': []})

    def test_handshake_and_read_only_tool_surface(self):
        source = io.StringIO('\n'.join(json.dumps(x) for x in [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'watson_status', 'arguments': {}}},
        ]))
        output = io.StringIO()
        serve(self.home, source, output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]['result']['protocolVersion'], '2025-06-18')
        tools = {t['name']: t for t in messages[1]['result']['tools']}
        self.assertEqual(set(tools), {'watson_status', 'watson_investigate'})
        props = tools['watson_investigate']['inputSchema']['properties']
        self.assertIn('issue', props)
        self.assertIn('number', props)
        self.assertFalse(messages[2]['result']['isError'])

    def test_arbitrary_actions_and_repositories_rejected(self):
        for name, arguments in [('merge', {}), ('deliver', {}),
                                ('watson_investigate', {'number': 1, 'repo': 'other/repo'}),
                                ('watson_investigate', {'number': '1; echo bad'}),
                                ('watson_investigate', {'number': True}),
                                ('watson_investigate', {'issue': 12, 'number': 12}),
                                ('watson_investigate', {})]:
            response = dispatch(self.home, {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
            self.assertTrue(response['isError'], msg=(name, arguments, response))

    def test_investigate_accepts_url_via_issue_argument(self):
        fake = {'run_id': 1, 'cached': True, 'result': {'summary': 'ok'}}
        with patch('watson.mcp.triage', return_value=fake) as triage, \
             patch('watson.mcp.GitHub'), \
             patch('watson.mcp.Codex'):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'issue': 'https://github.com/demo/repo/issues/42'},
                },
            })
        self.assertFalse(response['isError'], response)
        triage.assert_called_once()
        self.assertEqual(triage.call_args.args[4], 42)
        payload = json.loads(response['content'][0]['text'])
        self.assertEqual(payload['run_id'], 1)

    def test_investigate_rejects_foreign_repo_url_in_portuguese(self):
        response = dispatch(self.home, {
            'method': 'tools/call',
            'params': {
                'name': 'watson_investigate',
                'arguments': {'issue': 'https://github.com/other/place/issues/1'},
            },
        })
        self.assertTrue(response['isError'])
        text = response['content'][0]['text']
        self.assertIn('demo/repo', text)
        self.assertNotIn('Docker', text)
        self.assertNotIn('PAT', text)


if __name__ == '__main__':
    unittest.main()
