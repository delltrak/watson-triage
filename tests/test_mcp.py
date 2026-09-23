import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watson.capabilities import (
    capabilities_report,
    check_github,
    detect_language,
    github_missing_message,
    require_github,
)
from watson.core import Store, WatsonError, private_json
from watson.issue_ref import parse_issue_ref, resolve_issue_number, resolve_issue_ref
from watson.mcp import TOOLS, dispatch, serve
from watson.oauth_connect import _parse_claude, _parse_codex, _strip_ansi


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

    def test_resolve_issue_ref_uses_url_or_configured_repository(self):
        config = {'repository': 'demo/repo'}
        self.assertEqual(resolve_issue_ref('#9', config), ('demo/repo', 9))
        self.assertEqual(resolve_issue_number('#9', config), 9)
        self.assertEqual(
            resolve_issue_ref('https://github.com/demo/repo/issues/9', config),
            ('demo/repo', 9))
        self.assertEqual(
            resolve_issue_ref('https://github.com/other/repo/issues/9', config),
            ('other/repo', 9))
        self.assertEqual(
            resolve_issue_number('https://github.com/other/repo/issues/9', config), 9)

    def test_repo_homepage_asks_for_issue_link(self):
        for url in [
            'https://github.com/delltrak/comercial-uniao',
            'https://github.com/delltrak/comercial-uniao/',
            'https://www.github.com/delltrak/comercial-uniao?tab=readme',
        ]:
            with self.assertRaises(WatsonError) as ctx:
                parse_issue_ref(url)
            msg = str(ctx.exception)
            self.assertIn('issue', msg.lower())
            self.assertIn('/issues/', msg)
            self.assertNotIn('PAT', msg)


class CapabilityTests(unittest.TestCase):
    def test_github_missing_messages_bilingual_and_junior_friendly(self):
        github = {'ok': False, 'reason': 'not_authenticated'}
        both = github_missing_message(github, None)
        self.assertIn('GitHub is not connected', both)
        self.assertIn('O GitHub ainda não está conectado', both)
        self.assertNotIn('Docker', both)
        self.assertNotIn('PAT', both)
        self.assertNotIn('compose', both.lower())
        en = github_missing_message(github, 'en')
        pt = github_missing_message(github, 'pt')
        self.assertIn('github-credentials', en)
        self.assertIn('github-credentials', pt)
        self.assertNotIn('O GitHub ainda', en)
        self.assertNotIn('GitHub is not connected', pt)
        # iMessage-friendly: blank line between numbered setup steps
        self.assertIn('1)', en)
        self.assertIn('\n\n2)', en)
        self.assertIn('\n\n3)', en)
        self.assertIn('\n\n2)', pt)
        self.assertIn('\n\n3)', pt)

    def test_check_github_reports_missing_cli(self):
        with patch('watson.capabilities.shutil.which', return_value=None):
            result = check_github()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'cli_missing')

    def test_check_github_ok_via_auth_status(self):
        class Result:
            returncode = 0
            stdout = ''
            stderr = 'Logged in to github.com account delltrak (keyring)'

        def fake_run(cmd, **kwargs):
            return Result()

        with patch('watson.capabilities.shutil.which', return_value='/usr/bin/gh'):
            result = check_github(run=fake_run)
        self.assertTrue(result['ok'])
        self.assertEqual(result['login'], 'delltrak')

    def test_capabilities_report_never_all_ok_when_gh_missing(self):
        fake_gh = {'ok': False, 'reason': 'not_authenticated', 'connected': False, 'login': None}
        fake_codex = {'ok': True, 'reason': 'cli_present', 'connected': True}
        fake_claude = {'ok': True, 'reason': 'cli_present', 'connected': True}
        with patch('watson.capabilities.check_github', return_value=fake_gh), \
             patch('watson.capabilities.check_codex', return_value=fake_codex), \
             patch('watson.capabilities.check_claude', return_value=fake_claude):
            report = capabilities_report(language='en')
        self.assertFalse(report['ready_to_investigate'])
        self.assertFalse(report['github']['connected'])
        self.assertIn('Not ready to investigate', report['summary'])
        self.assertNotIn('tudo ok', report['summary'].lower())
        self.assertIn('not connected', report['summary'].lower())
        # Blank lines between 1/2/3 status items for plain iMessage
        self.assertIn('1. **GitHub**', report['summary'])
        self.assertIn('\n\n2. **Codex CLI**', report['summary'])
        self.assertIn('\n\n3. **Claude Code CLI**', report['summary'])
        self.assertIn('setup', report)
        self.assertIn('\n\n2)', report['setup'])
        self.assertIn('onboarding', report)
        self.assertIn('engineering teammate', report['onboarding'])
        self.assertIn('**GitHub**', report['onboarding'])
        self.assertNotIn('Plow assistant', report['onboarding'])
        self.assertIn('/help', report['onboarding'])

    def test_detect_language_short_pt_greetings(self):
        for text in ('Oi', 'oi!', 'olá', 'Olá', 'bom dia'):
            self.assertEqual(detect_language(text), 'pt', text)
        for text in ('hey', 'hi', 'hello'):
            self.assertEqual(detect_language(text), 'en', text)

    def test_onboarding_pt_cold_start_shape(self):
        fake_gh = {'ok': False, 'reason': 'not_authenticated', 'connected': False, 'login': None}
        fake_codex = {'ok': False, 'reason': 'not_authenticated', 'connected': False}
        fake_claude = {'ok': False, 'reason': 'cli_missing', 'connected': False}
        with patch('watson.capabilities.check_github', return_value=fake_gh), \
             patch('watson.capabilities.check_codex', return_value=fake_codex), \
             patch('watson.capabilities.check_claude', return_value=fake_claude):
            report = capabilities_report(language='pt')
        text = report['onboarding']
        self.assertIn('colega de engenharia', text)
        self.assertNotIn('Plow assistant', text)
        self.assertNotIn('Deltrak', text)
        self.assertIn('Por enquanto ainda não consigo investigar', text)
        self.assertIn('**GitHub**', text)
        self.assertIn('**Codex CLI**', text)
        self.assertIn('**Claude Code CLI**', text)
        self.assertIn('\n\n1. **GitHub**', text)
        self.assertIn('\n\n2. **Codex CLI**', text)
        self.assertIn('\n\n3. **Claude Code CLI**', text)
        self.assertIn('github-credentials', text)
        self.assertIn('draft PR', text)
        self.assertIn('/help', text)
        self.assertIn('Nunca faço merge', text)
        self.assertIn('pede pra conectar aqui no chat', text)
        self.assertNotIn('conectar uma vez nesta linha', text)


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
        with patch('watson.mcp.capabilities_report', return_value={
            'github': {'connected': False, 'reason': 'cli_missing', 'login': None},
            'codex': {'connected': False, 'reason': 'cli_missing'},
            'ready_to_investigate': False,
            'summary': 'Not ready',
            'messages': {'en': 'Not ready', 'pt': 'Não pronto'},
            'setup': 'connect please',
        }):
            serve(self.home, source, output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]['result']['protocolVersion'], '2025-06-18')
        tools = {t['name']: t for t in messages[1]['result']['tools']}
        self.assertEqual(set(tools), {
            'watson_status', 'watson_investigate',
            'watson_connect_codex', 'watson_connect_claude',
        })
        props = tools['watson_investigate']['inputSchema']['properties']
        self.assertIn('issue', props)
        self.assertIn('number', props)
        self.assertIn('language', props)
        self.assertFalse(messages[2]['result']['isError'])
        payload = json.loads(messages[2]['result']['content'][0]['text'])
        self.assertFalse(payload['github_connected'])
        self.assertFalse(payload['ready_to_investigate'])

    def test_status_reports_missing_github_in_portuguese(self):
        fake = {
            'github': {'connected': False, 'reason': 'not_authenticated', 'login': None},
            'codex': {'connected': False, 'reason': 'cli_missing'},
            'ready_to_investigate': False,
            'summary': 'Ainda não dá para investigar: falta o GitHub.',
            'messages': {'pt': 'Ainda não dá para investigar: falta o GitHub.'},
            'setup': 'O GitHub ainda não está conectado',
            'onboarding': 'Oi — sou o Watson, seu colega de engenharia.\n\n/help',
        }
        with patch('watson.mcp.capabilities_report', return_value=fake) as caps:
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {'name': 'watson_status', 'arguments': {'language': 'pt'}},
            })
        caps.assert_called_once()
        self.assertEqual(caps.call_args.kwargs.get('language'), 'pt')
        self.assertFalse(response['isError'])
        payload = json.loads(response['content'][0]['text'])
        self.assertFalse(payload['ready_to_investigate'])
        self.assertIn('falta o GitHub', payload['status_summary'])
        self.assertIn('O GitHub ainda não está conectado', payload['setup'])
        self.assertIn('colega de engenharia', payload['onboarding'])

    def test_investigate_preflight_blocks_when_github_missing(self):
        with patch('watson.mcp.require_github', side_effect=WatsonError(
                'GitHub is not connected yet, so I cannot investigate issues.\n\n'
                '---\n\n'
                'O GitHub ainda não está conectado, então não consigo investigar issues.')):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'number': 1},
                },
            })
        self.assertTrue(response['isError'])
        text = response['content'][0]['text']
        self.assertIn('GitHub is not connected', text)
        self.assertIn('O GitHub ainda não está conectado', text)
        self.assertNotIn('Docker', text)
        self.assertNotIn('PAT', text)

    def test_investigate_preflight_language_en(self):
        with patch('watson.mcp.require_github', side_effect=WatsonError(
                'GitHub is not connected yet, so I cannot investigate issues.')) as req:
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'number': 3, 'language': 'en'},
                },
            })
        req.assert_called_once()
        self.assertEqual(req.call_args.kwargs.get('language'), 'en')
        self.assertTrue(response['isError'])
        self.assertIn('GitHub is not connected', response['content'][0]['text'])

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
        with patch('watson.mcp.require_github', return_value={'ok': True}), \
             patch('watson.mcp.triage', return_value=fake) as triage, \
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
        self.assertEqual(triage.call_args.kwargs.get('repo'), 'demo/repo')
        payload = json.loads(response['content'][0]['text'])
        self.assertEqual(payload['run_id'], 1)

    def test_investigate_uses_foreign_repo_from_issue_url(self):
        fake = {'run_id': 2, 'cached': True, 'result': {'summary': 'ok', 'repository': 'other/place'}}
        with patch('watson.mcp.require_github', return_value={'ok': True}), \
             patch('watson.mcp.triage', return_value=fake) as triage, \
             patch('watson.mcp.GitHub') as gh, \
             patch('watson.mcp.Codex'):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'issue': 'https://github.com/other/place/issues/1'},
                },
            })
        self.assertFalse(response['isError'], response)
        triage.assert_called_once()
        self.assertEqual(triage.call_args.args[4], 1)
        self.assertEqual(triage.call_args.kwargs.get('repo'), 'other/place')
        gh.assert_called_once()
        repos = gh.call_args.args[0]
        self.assertEqual(repos[0], 'other/place')
        self.assertIn('demo/repo', repos)

    def test_investigate_repo_homepage_asks_for_issue(self):
        with patch('watson.mcp.require_github', return_value={'ok': True}):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'issue': 'https://github.com/delltrak/comercial-uniao'},
                },
            })
        self.assertTrue(response['isError'])
        text = response['content'][0]['text']
        self.assertIn('/issues/', text)
        self.assertNotIn('Internal failure', text)
        self.assertNotIn('watson-triage', text)
        self.assertNotIn('Docker', text)

    def test_arguments_json_string_coerced(self):
        fake = {'run_id': 3, 'cached': True, 'result': {'summary': 'ok'}}
        with patch('watson.mcp.require_github', return_value={'ok': True}), \
             patch('watson.mcp.triage', return_value=fake) as triage, \
             patch('watson.mcp.GitHub'), \
             patch('watson.mcp.Codex'):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': json.dumps({'issue': 'https://github.com/demo/repo/issues/5'}),
                },
            })
        self.assertFalse(response['isError'], response)
        self.assertEqual(triage.call_args.args[4], 5)

    def test_internal_failure_includes_exception_type(self):
        err = io.StringIO()
        with patch('watson.mcp.require_github', return_value={'ok': True}), \
             patch('watson.mcp.resolve_issue_ref', side_effect=RuntimeError('boom')), \
             patch('watson.mcp.sys.stderr', err):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_investigate',
                    'arguments': {'number': 1},
                },
            })
        self.assertTrue(response['isError'])
        self.assertEqual(
            response['content'][0]['text'],
            'Internal failure (RuntimeError). Check the local install.')
        self.assertIn('RuntimeError', err.getvalue())

    def test_status_does_not_take_worker_lock(self):
        with patch('watson.mcp.capabilities_report', return_value={
            'github': {'connected': True, 'reason': 'ok', 'login': 'x'},
            'codex': {'connected': False, 'reason': 'cli_missing'},
            'ready_to_investigate': False,
            'summary': 'ok-ish',
        }), patch.object(Store, 'lock', side_effect=AssertionError('status must not lock')):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {'name': 'watson_status', 'arguments': {}},
            })
        self.assertFalse(response['isError'], response)

    def test_store_permission_error_becomes_watson_error(self):
        from watson.core import HOME_NOT_WRITABLE, Store as RealStore
        with patch.object(Path, 'mkdir', side_effect=PermissionError('denied')):
            with self.assertRaises(WatsonError) as ctx:
                RealStore(self.home / 'nope')
        self.assertEqual(str(ctx.exception), HOME_NOT_WRITABLE)

        with patch('watson.mcp.Store', side_effect=WatsonError(HOME_NOT_WRITABLE)):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {'name': 'watson_status', 'arguments': {}},
            })
        self.assertTrue(response['isError'])
        self.assertIn('not writable', response['content'][0]['text'])
        self.assertIn('não tem permissão', response['content'][0]['text'])
        self.assertNotIn('Internal failure', response['content'][0]['text'])

    def test_require_github_raises_curated_error(self):
        with patch('watson.capabilities.check_github', return_value={
            'ok': False, 'reason': 'not_authenticated', 'connected': False, 'login': None,
        }):
            with self.assertRaises(WatsonError) as ctx:
                require_github(language='pt')
        self.assertIn('O GitHub ainda não está conectado', str(ctx.exception))



class OAuthConnectParseTests(unittest.TestCase):
    def test_strip_ansi_and_parse_codex_device_output(self):
        raw = (
            'Welcome\n'
            '\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n'
            'Enter this one-time code\n'
            '\x1b[94mTRWW-KJ1JA\x1b[0m\n'
        )
        clean = _strip_ansi(raw)
        self.assertNotIn('\x1b', clean)
        url, code = _parse_codex(raw)
        self.assertEqual(url, 'https://auth.openai.com/codex/device')
        self.assertEqual(code, 'TRWW-KJ1JA')

    def test_parse_claude_oauth_url(self):
        raw = (
            'Opening browser to sign in…\n'
            'If the browser didn\'t open, visit: '
            'https://claude.com/cai/oauth/authorize?code=true&client_id=abc&'
            'redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback\n'
            'Paste code here if prompted > '
        )
        url = _parse_claude(raw)
        self.assertTrue(url.startswith('https://claude.com/cai/oauth/authorize'))
        self.assertIn('platform.claude.com', url)


class OAuthConnectToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        store = Store(self.home)
        store.db.close()
        private_json(self.home / 'config.json', {
            'repository': 'demo/repo', 'assignee': 'owner', 'related_repositories': []})

    def test_tools_list_includes_connect(self):
        names = {t['name'] for t in TOOLS}
        self.assertIn('watson_connect_codex', names)
        self.assertIn('watson_connect_claude', names)

    def test_connect_codex_already_authenticated(self):
        with patch('watson.oauth_connect.check_codex', return_value={
            'ok': True, 'reason': 'authenticated', 'connected': True,
        }):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {'name': 'watson_connect_codex', 'arguments': {'language': 'pt'}},
            })
        self.assertFalse(response['isError'], response)
        payload = json.loads(response['content'][0]['text'])
        self.assertTrue(payload['already_authenticated'])
        self.assertIn('já está conectado', payload['message'])

    def test_connect_codex_returns_url_from_started_login(self):
        class FakeProc:
            pid = 4242
            stdin = None

            def __init__(self, *a, **k):
                pass

        def fake_start(home, provider, cmd):
            log = Path(home) / 'oauth' / f'{provider}.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                'Follow these steps\n'
                'https://auth.openai.com/codex/device\n'
                'Enter this one-time code\n'
                'ABCD-EFGH1\n'
            )
            return FakeProc()

        with patch('watson.oauth_connect.check_codex', return_value={
            'ok': False, 'reason': 'not_authenticated', 'connected': False,
        }), patch('watson.oauth_connect._start_process', side_effect=fake_start),              patch('watson.oauth_connect._pid_alive', return_value=True),              patch('watson.oauth_connect._spawn_stdin_holder'):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_connect_codex',
                    'arguments': {'language': 'en'},
                },
            })
        self.assertFalse(response['isError'], response)
        payload = json.loads(response['content'][0]['text'])
        self.assertEqual(payload['status'], 'waiting_browser')
        self.assertEqual(payload['auth_url'], 'https://auth.openai.com/codex/device')
        self.assertEqual(payload['user_code'], 'ABCD-EFGH1')
        self.assertIn('https://auth.openai.com/codex/device', payload['message'])
        self.assertIn('ABCD-EFGH1', payload['message'])

    def test_connect_claude_returns_url(self):
        class FakeProc:
            pid = 5252
            stdin = type('S', (), {'close': lambda self: None, 'fileno': lambda self: 1})()

            def __init__(self, *a, **k):
                pass

        def fake_start(home, provider, cmd):
            log = Path(home) / 'oauth' / f'{provider}.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(
                'visit: https://claude.com/cai/oauth/authorize?code=true&x=1\n'
                'Paste code here if prompted > '
            )
            return FakeProc()

        with patch('watson.oauth_connect.check_claude', return_value={
            'ok': False, 'reason': 'not_authenticated', 'connected': False,
        }), patch('watson.oauth_connect._start_process', side_effect=fake_start),              patch('watson.oauth_connect._pid_alive', return_value=True),              patch('watson.oauth_connect._spawn_stdin_holder'):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_connect_claude',
                    'arguments': {'language': 'pt'},
                },
            })
        self.assertFalse(response['isError'], response)
        payload = json.loads(response['content'][0]['text'])
        self.assertEqual(payload['status'], 'waiting_code')
        self.assertTrue(payload['auth_url'].startswith('https://claude.com/'))
        self.assertIn('https://claude.com/', payload['message'])
        self.assertIn('código', payload['message'].lower())

    def test_connect_claude_rejects_api_key_looking_code(self):
        # Seed a pending session
        oauth = self.home / 'oauth'
        oauth.mkdir(parents=True, exist_ok=True)
        (oauth / 'claude.json').write_text(json.dumps({
            'provider': 'claude', 'pid': 999, 'status': 'waiting_code',
            'auth_url': 'https://claude.com/x', 'needs_paste_code': True,
        }))
        with patch('watson.oauth_connect.check_claude', return_value={
            'ok': False, 'reason': 'not_authenticated', 'connected': False,
        }), patch('watson.oauth_connect._pid_alive', return_value=True):
            response = dispatch(self.home, {
                'method': 'tools/call',
                'params': {
                    'name': 'watson_connect_claude',
                    'arguments': {'language': 'en', 'code': 'sk-ant-api03-secret'},
                },
            })
        self.assertTrue(response['isError'])
        self.assertIn('API key', response['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
