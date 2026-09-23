import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watson import roster
from watson.analysis import Claude, Codex, Crew, RESULT_SCHEMA, triage
from watson.core import Store, WatsonError
from watson.review import REVIEW_SCHEMA, note

ALL = {'codex': True, 'claude': True}
_REAL_CODEX_MODELS = roster.codex_models


def setUpModule():
    # Hermetic: never read the developer's ~/.codex model cache.
    p = patch.object(roster, 'codex_models', lambda home=None: dict(roster.CODEX_FALLBACK_MODELS))
    p.start()
    unittest.addModuleCleanup(p.stop)


class RosterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_default_squad_and_fallback(self):
        squad = roster.load(self.home)
        self.assertEqual(squad['investigator'], {'engine': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'})
        self.assertEqual(squad['reviewer']['engine'], 'claude')
        effective = roster.resolve(squad, {'codex': True, 'claude': False})
        self.assertEqual(effective['reviewer']['engine'], 'codex')
        self.assertEqual(effective['reviewer']['fallback_from'], 'claude')
        self.assertTrue(roster.resolve(squad, {'codex': False, 'claude': False})['investigator']['unavailable'])

    def test_change_reset_and_validation(self):
        before, after, role = roster.change(self.home, 'revisor', 'opus', 'high')
        self.assertEqual(role, 'reviewer')
        self.assertEqual(after['reviewer'], {'engine': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high'})
        self.assertEqual(roster.load(self.home)['reviewer']['model'], 'claude-opus-5-5')
        roster.change(self.home, 'investigator', 'sonnet')  # crossing teams is allowed
        self.assertEqual(roster.load(self.home)['investigator']['engine'], 'claude')
        with self.assertRaises(WatsonError):
            roster.change(self.home, 'revisor', 'haiku', 'max')  # haiku has no max effort
        with self.assertRaises(WatsonError):
            roster.change(self.home, 'chef', 'opus')
        with self.assertRaises(WatsonError):
            roster.change(self.home, 'revisor', 'gpt-9-mega')
        roster.change(self.home, reset=True)
        self.assertEqual(roster.load(self.home), roster.DEFAULT)
        self.assertEqual(oct((self.home / 'roster.json').stat().st_mode & 0o777), '0o600')

    def test_chat_requests(self):
        cases = {
            'minha tropa': ('show', 'pt'), 'Minha tropa?': ('show', 'pt'), 'my squad': ('show', 'en'),
            'tropa padrão': ('reset', 'pt'), 'default squad': ('reset', 'en'),
            'coloca o revisor no opus': ('set', 'pt'), 'revisor no opus high': ('set', 'pt'),
            'investigador no sol high': ('set', 'pt'), 'put the reviewer on opus': ('set', 'en'),
            'revisor no gpt-6-sol': ('set', 'pt'), 'revisor esforço max': ('set', 'pt'),
        }
        for text, (action, lang) in cases.items():
            request = roster.parse_request(text, self.home)
            self.assertEqual((request['action'], request['lang']), (action, lang), text)
        for text in ('investiga a 71', 'oi', 'tudo bem', 'o revisor achou algo?', 'x' * 200):
            self.assertIsNone(roster.parse_request(text, self.home), text)

    def test_more_ways_to_ask_and_questions_do_not_change_anything(self):
        for text in ('revisor: opus', 'reviewer -> opus', 'coloca o revisor no opus por favor',
                     'put the reviewer on opus please', 'reviewer effort to max', 'revisor no máximo',
                     'troca o revisor pelo opus', 'investigador sol alto'):
            self.assertEqual(roster.parse_request(text, self.home)['action'], 'set', text)
        for text in ('o revisor no opus?', 'What’s my squad?'):
            self.assertEqual(roster.parse_request(text, self.home)['action'], 'show', text)
        for text in ('meu time', 'minha equipe', 'squad', 'my crew', 'revisor alto?'):
            self.assertIsNone(roster.parse_request(text, self.home), text)
        self.assertEqual(roster.parse_request('reviewer as opus', self.home)['lang'], 'en')
        self.assertEqual(roster.parse_request('revisor effort max', self.home)['lang'], 'pt')

    def test_corrupt_roster_file_falls_back_to_defaults(self):
        for content in ('{"roles": []}', '{"roles": {"reviewer": {"engine": "claude"}}}',
                        '{"roles": {"reviewer": {"engine": "codex", "model": "claude-opus-5-5", "effort": "high"}}}',
                        'not json'):
            (self.home / 'roster.json').write_text(content)
            self.assertEqual(roster.load(self.home), roster.DEFAULT, content)

    def test_codex_cache_format_changes_never_break(self):
        with tempfile.TemporaryDirectory() as folder:
            codex = Path(folder) / '.codex'
            codex.mkdir()
            (codex / 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': 'gpt-7', 'supported_reasoning_levels': ['low', 'high']},
                {'slug': 'hidden-one', 'visibility': 'hide', 'supported_reasoning_levels': []}]}))
            with patch.object(Path, 'home', return_value=Path(folder) / 'nobody'):
                models = _REAL_CODEX_MODELS(Path(folder) / 'watson')
        self.assertEqual(models['gpt-7'], ('low', 'high'))
        self.assertNotIn('hidden-one', models)
        self.assertIn('gpt-6-sol', models)  # known defaults are always accepted

    def test_cross_review_line_is_honest(self):
        same = roster.render({**roster.DEFAULT, 'reviewer': dict(roster.DEFAULT['investigator'])}, ALL, 'pt')
        self.assertIn('Agora o mesmo modelo investiga e revisa (Time Codex, GPT-6-Sol)', same)
        self.assertNotIn('quem investiga não é quem confere', same)
        codex_out = roster.render(roster.DEFAULT, {'codex': False, 'claude': True}, 'en')
        self.assertIn('Right now the same model investigates and reviews (Team Claude, Sonnet 5)', codex_out)

    def test_reply_copy(self):
        text = roster.reply(self.home, 'coloca o revisor no opus', ALL)
        self.assertIn('✅ Revisor: Sonnet 5 (médio) → Opus 5.5 (médio).', text)
        self.assertIn('**Revisor** — Time Claude, Opus 5.5 (médio)', text)
        en = roster.reply(self.home, 'my squad', {'codex': True, 'claude': False})
        self.assertIn('**Reviewer** — Team Claude, Opus 5.5 (medium) — not logged in; meanwhile Team Codex '
                      'covers with GPT-6-Luna (high)', en)
        self.assertIn('**Investigator** — Team Codex, GPT-6-Sol (medium)', en)
        bad = roster.reply(self.home, 'revisor no haiku max', ALL)
        self.assertIn('Haiku 4.5 não aceita esforço "máximo"', bad)
        self.assertNotIn('---', bad)
        for copy_text in (text, en):
            self.assertNotIn('`', copy_text)


class _Completed:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ''


class ClaudeEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def _run(self, payload, calls):
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return _Completed(json.dumps(payload))
        return run

    def test_read_only_flags_scrubbed_env_and_structured_output(self):
        calls = []
        engine = Claude(self.home, 'claude-sonnet-5', effort='low',
                        run=self._run({'is_error': False, 'subtype': 'success', 'permission_denials': [],
                                       'structured_output': {'ok': True}, 'usage': {'input_tokens': 3}}, calls))
        with patch.dict(os.environ, {'GH_TOKEN': 'secret', 'PLOW_AGENT_TOKEN': 'secret', 'HOME': '/h'}):
            self.assertEqual(engine.ask('x', {'a': 1}, {'type': 'object'}, 'lbl'), {'ok': True})
        cmd, kwargs = calls[0]
        for flag in ('-p', '--restricted', '--strict-mcp-config', '--safe-mode', '--no-session-persistence'):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertEqual(cmd[cmd.index('--effort') + 1], 'low')
        self.assertNotIn('GH_TOKEN', kwargs['env'])
        self.assertNotIn('PLOW_AGENT_TOKEN', kwargs['env'])
        self.assertTrue(str(kwargs['cwd']).startswith(str(self.home)))

    def test_tool_attempt_or_error_is_rejected(self):
        for payload in ({'is_error': False, 'permission_denials': [{'tool': 'Bash'}], 'structured_output': {}},
                        {'is_error': True, 'result': 'Not logged in'},
                        {'is_error': False, 'structured_output': None}):
            engine = Claude(self.home, 'claude-sonnet-5', run=self._run(payload, []))
            with self.assertRaises(WatsonError):
                engine.ask('x', {}, {'type': 'object'}, 'lbl')

    def test_crew_dispatch_and_signature(self):
        effective = roster.resolve(roster.DEFAULT, ALL)
        crew = Crew(self.home, effective)
        self.assertIsInstance(crew.for_role('investigator'), Codex)
        self.assertIsInstance(crew.for_role('reviewer'), Claude)
        self.assertEqual(crew.for_role('reviewer').timeout, 180)
        self.assertEqual(crew.for_role('selector').effort, 'max')
        other = Crew(self.home, roster.resolve(roster.DEFAULT, {'codex': True, 'claude': False}))
        self.assertNotEqual(crew.signature(), other.signature())


ISSUE = {'repository': 'demo/repo', 'number': 7, 'title': 'Falha no calendário', 'body': '',
         'state': 'open', 'state_reason': None, 'author': 'author', 'assignees': ['owner'],
         'url': 'https://github.com/demo/repo/issues/7', 'updated_at': '2026-09-15T00:00:00Z', 'comments': []}
RESULT = {'status': 'investigate', 'summary': 'Resumo.', 'voice_script': 'Texto.',
          'findings': [{'claim': 'A falha está no calendário.', 'evidence_ids': ['source:1'], 'certainty': 'observed'},
                       {'claim': 'O autor relata erro no sábado.', 'evidence_ids': ['issue'], 'certainty': 'reported'},
                       {'claim': 'O CI está vermelho.', 'evidence_ids': ['issue'], 'certainty': 'observed'}],
          'next_steps': [], 'questions_for_author': [], 'branch_recommendation': 'candidate', 'limitations': [],
          'closure': {'assessment': 'not_applicable', 'pull_numbers': [], 'evidence_ids': [], 'reason': ''}}


class FakeGitHub:
    def issue(self, repo, number): return copy.deepcopy(ISSUE)
    def references(self, issue): return [], []
    def source_index(self, repo): return {'repository': repo, 'sha': 'a' * 40, 'paths': ['src/cal.ts']}
    def file(self, repo, path, sha):
        return {'repository': repo, 'path': path, 'sha': sha, 'url': 'u', 'content': '1: x'}


class FakeEngine:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def ask(self, instruction, payload, schema, label):
        self.calls.append((schema, label))
        if self.fail:
            raise WatsonError('boom')
        if schema == RESULT_SCHEMA:
            return copy.deepcopy(RESULT)
        if schema == REVIEW_SCHEMA:
            return self.answer
        return {'paths': ['src/cal.ts'], 'reason': 'x'}


class FakeCrew:
    def __init__(self, reviewer, effective=None, left=None):
        self.engines = {'selector': FakeEngine(), 'investigator': FakeEngine(), 'reviewer': reviewer}
        self.effective = effective or roster.resolve(roster.DEFAULT, ALL)
        self.left = left

    def for_role(self, role): return self.engines[role]
    def signature(self): return roster.signature(self.effective)
    def remaining(self): return self.left


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.addCleanup(self.store.db.close)
        self.config = {'repository': 'demo/repo', 'assignee': 'owner', 'related_repositories': []}

    def test_verdicts_are_applied_by_code(self):
        answer = {'verdicts': [{'index': 0, 'verdict': 'supported', 'note': 'ok'},
                               {'index': 1, 'verdict': 'weak', 'note': 'meh'},
                               {'index': 2, 'verdict': 'unsupported', 'note': 'nada sobre CI'},
                               {'index': 9, 'verdict': 'supported', 'note': 'out of range'}],
                  'notes': ['Faltou olhar o fuso horário.']}
        crew = FakeCrew(FakeEngine(answer))
        result = triage(self.store, FakeGitHub(), crew, self.config, 7)['result']
        self.assertEqual([f['claim'] for f in result['findings']],
                         ['A falha está no calendário.', 'O autor relata erro no sábado.'])
        self.assertEqual(result['findings'][1]['certainty'], 'reported')  # weak keeps non-observed certainty
        review = result['review']
        self.assertEqual((review['supported'], review['weak'], len(review['removed'])), (1, 1, 1))
        self.assertEqual(review['note']['pt'], 'Revisado pelo Time Claude (Sonnet 5): 1 de 3 achados confirmados, '
                                               '1 com ressalva, 1 removido por falta de evidência.')
        self.assertEqual(review['note']['en'], 'Reviewed by Team Claude (Sonnet 5): 1 of 3 findings confirmed, '
                                               '1 with caveats, 1 removed for lack of evidence.')

    def test_failed_review_never_blocks_the_investigation(self):
        crew = FakeCrew(FakeEngine(fail=True))
        result = triage(self.store, FakeGitHub(), crew, self.config, 7)['result']
        self.assertEqual(len(result['findings']), 3)
        self.assertEqual(result['review']['status'], 'skipped')
        self.assertIn('não rodou desta vez', result['review']['note']['pt'])

    def test_fallback_is_disclosed(self):
        effective = roster.resolve(roster.DEFAULT, {'codex': True, 'claude': False})
        crew = FakeCrew(FakeEngine({'verdicts': [], 'notes': []}), effective)
        result = triage(self.store, FakeGitHub(), crew, self.config, 7)['result']
        self.assertIn('Time Codex (GPT-6-Luna), cobrindo o Time Claude, que está sem login',
                      result['review']['note']['pt'])

    def test_squad_change_invalidates_cache(self):
        first = triage(self.store, FakeGitHub(), FakeCrew(FakeEngine({'verdicts': [], 'notes': []})), self.config, 7)
        again = triage(self.store, FakeGitHub(), FakeCrew(FakeEngine({'verdicts': [], 'notes': []})), self.config, 7)
        self.assertTrue(again['cached'])
        other = roster.resolve({**roster.DEFAULT, 'reviewer': {'engine': 'claude', 'model': 'claude-opus-5-5',
                                                               'effort': 'high'}}, ALL)
        changed = triage(self.store, FakeGitHub(), FakeCrew(FakeEngine({'verdicts': [], 'notes': []}), other),
                         self.config, 7)
        self.assertFalse(changed['cached'])
        self.assertNotEqual(first['run_id'], changed['run_id'])

    def test_no_time_left_skips_the_review_honestly(self):
        reviewer = FakeEngine({'verdicts': [], 'notes': []})
        result = triage(self.store, FakeGitHub(), FakeCrew(reviewer, left=30), self.config, 7)['result']
        self.assertEqual(result['review']['reason'], 'no_time')
        self.assertEqual([c for c in reviewer.calls if c[0] == REVIEW_SCHEMA], [])
        self.assertIn('não rodou desta vez', result['review']['note']['pt'])

    def test_reviewer_gets_the_full_cited_evidence(self):
        seen = {}

        class Recorder(FakeEngine):
            def ask(inner, instruction, payload, schema, label):
                if schema == REVIEW_SCHEMA:
                    seen.update(payload)
                return FakeEngine.ask(inner, instruction, payload, schema, label)

        class BigFile(FakeGitHub):
            def file(self, repo, path, sha):
                content = '\n'.join(f'{i}: linha {i}' for i in range(1, 6000))
                return {'repository': repo, 'path': path, 'sha': sha, 'url': 'u', 'content': content}

        triage(self.store, BigFile(), FakeCrew(Recorder({'verdicts': [], 'notes': []})), self.config, 7)
        self.assertIn('5999: linha 5999', seen['evidence']['source:1']['content'])

    def test_failed_review_is_retried_not_cached(self):
        first = triage(self.store, FakeGitHub(), FakeCrew(FakeEngine(fail=True)), self.config, 7)
        self.assertEqual(first['result']['review']['reason'], 'failed')
        reviewer = FakeEngine({'verdicts': [{'index': 0, 'verdict': 'supported', 'note': 'ok'}], 'notes': []})
        again = triage(self.store, FakeGitHub(), FakeCrew(reviewer), self.config, 7)
        self.assertFalse(again['cached'])
        self.assertEqual(again['run_id'], first['run_id'])
        self.assertEqual(again['result']['review']['status'], 'done')
        self.assertEqual(len([c for c in reviewer.calls if c[0] == REVIEW_SCHEMA]), 1)

    def test_investigator_fallback_and_same_team_are_disclosed(self):
        effective = roster.resolve(roster.DEFAULT, {'codex': False, 'claude': True})
        crew = FakeCrew(FakeEngine({'verdicts': [{'index': 0, 'verdict': 'supported', 'note': 'ok'}],
                                    'notes': []}), effective)
        text = triage(self.store, FakeGitHub(), crew, self.config, 7)['result']['review']['note']['pt']
        self.assertTrue(text.startswith('Investigado pelo Time Claude (Sonnet 5), cobrindo o Time Codex'))
        self.assertIn('O mesmo time investigou e revisou: não houve revisão cruzada.', text)

    def test_note_variants(self):
        base = {'status': 'done', 'team': 'claude', 'model': 'claude-sonnet-5', 'total': 3, 'supported': 0,
                'weak': 0, 'removed': []}
        self.assertIn('o revisor não avaliou os achados', note({**base, 'checked': 0}, 'pt'))
        self.assertIn('no support in the evidence for any finding',
                      note({**base, 'checked': 3, 'all_unsupported': True}, 'en'))

    def test_unavailable_reviewer_note(self):
        self.assertIn('nenhum time está logado',
                      note({'status': 'skipped', 'reason': 'unavailable', 'team': 'claude', 'model': None}, 'pt'))


class OwnerShortcutTests(unittest.TestCase):
    def test_squad_commands_and_greetings_share_the_owner_path(self):
        from watson.greeting import owner_shortcut
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch('watson.roster.connected_teams', return_value=ALL):
                text = owner_shortcut('minha tropa', home)
            self.assertIn('🪖 **Minha tropa**', text)
            with patch('watson.greeting.capabilities_report', return_value={'speak_this': 'Oi!'}) as caps:
                self.assertEqual(owner_shortcut('oi', home), 'Oi!')
            self.assertEqual(caps.call_args.kwargs['squad'], roster.DEFAULT)
            self.assertIsNone(owner_shortcut('investiga a 7', home))


if __name__ == '__main__':
    unittest.main()


class ChatPayloadTests(unittest.TestCase):
    def test_removed_claims_never_reach_the_chat(self):
        from watson.core import private_json
        from watson.mcp import dispatch
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            Store(home).db.close()
            private_json(home / 'config.json', {'repository': 'demo/repo', 'assignee': 'o',
                                                 'related_repositories': []})
            result = {'status': 'investigate', 'summary': 'O CI está vermelho.', 'voice_script': 'x',
                      'next_steps': ['y'], 'findings': [{'claim': 'ok', 'evidence_ids': ['issue'],
                                                         'certainty': 'observed'}],
                      'review': {'status': 'done', 'removed': [{'claim': 'O CI está vermelho.', 'note': 'n'}],
                                 'notes': ['texto livre'], 'weak': 0,
                                 'note': {'pt': 'Revisado pelo Time Claude (Sonnet 5): 1 de 2 achados '
                                                'confirmados, 1 removido por falta de evidência.', 'en': 'x'}}}
            with patch('watson.mcp.require_github'), patch('watson.mcp.GitHub'), patch('watson.mcp.Crew'), \
                 patch('watson.mcp.roster.connected_teams', return_value=ALL), \
                 patch('watson.mcp.triage', return_value={'run_id': 1, 'cached': False, 'result': result}):
                response = dispatch(home, {'method': 'tools/call', 'params': {
                    'name': 'watson_investigate', 'arguments': {'issue': '#7', 'language': 'pt'}}})
            text = response['content'][0]['text']
            self.assertNotIn('O CI está vermelho', text)
            self.assertNotIn('texto livre', text)
            payload = json.loads(text)
            self.assertEqual(payload['result']['review']['removed_count'], 1)
            self.assertTrue(payload['review_note'].startswith('Revisado pelo Time Claude'))
            self.assertIn('describe ONLY result.findings', payload['instruction'])
