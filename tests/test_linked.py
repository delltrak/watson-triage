import copy
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from watson.analysis import RESULT_SCHEMA, triage, validate_result
from watson.core import Store, WatsonError
from watson.github import GitHub
from watson.linked import decide, link_kind, say, targets, verdicts

REPO = 'demo/repo'
MERGE, HEAD = '02c6d0f00da2ced85595e216187610ca8e944a27', '320815c2fbd849ccf0a488b091a31e7a9589dcd6'
PR_BODY = ('## Issue\n\nRefs #71\n\n## Rastreabilidade\n\n'
           '- Relaciona-se a #62/#39, sem encerrá-los.\n')  # trimmed from delltrak/comercial-uniao#72


def cross_ref(number, *, merged_at='2026-09-22T15:10:36Z', state='closed', body=PR_BODY, repo=REPO,
              association='OWNER', draft=False, is_pr=True):
    # Shape recorded from GET issues/71/timeline: no id/url on cross-referenced events.
    issue = {'number': number, 'title': 'Interface do atendimento', 'state': state, 'draft': draft,
             'body': body, 'html_url': f'https://github.com/{repo}/pull/{number}',
             'user': {'login': 'delltrak'}, 'author_association': association,
             'repository': {'full_name': repo, 'private': True}}
    if is_pr:
        issue['pull_request'] = {'url': f'https://api.github.com/repos/{repo}/pulls/{number}',
                                 'html_url': issue['html_url'], 'merged_at': merged_at}
    return {'event': 'cross-referenced', 'created_at': '2026-09-22T13:28:14Z', 'updated_at': '2026-09-22T13:28:14Z',
            'actor': {'login': 'delltrak'}, 'source': {'type': 'issue', 'issue': issue}}


def pull(number, *, merged=True, base='main', merge_sha=MERGE, head_sha=HEAD):
    return {'number': number, 'state': 'closed' if merged else 'open', 'merged': merged, 'draft': False,
            'merged_at': '2026-09-22T15:10:36Z' if merged else None,
            'merge_commit_sha': merge_sha if merged else None, 'merged_by': {'login': 'delltrak'},
            'base': {'ref': base, 'repo': {'full_name': REPO, 'default_branch': 'main'}},
            'head': {'ref': 'ui/icones-menu', 'sha': head_sha}}


VERDICT_COMMENTS = [
    {'id': 1, 'user': {'login': 'delltrak'}, 'author_association': 'OWNER',
     'html_url': 'https://github.com/demo/repo/pull/72#issuecomment-1',
     'body': '## Revisão independente — commit `05884c2`\n\n**Veredito: APROVAR COM RESSALVAS.**'},
    {'id': 2, 'user': {'login': 'delltrak'}, 'author_association': 'OWNER',
     'html_url': 'https://github.com/demo/repo/pull/72#issuecomment-2',
     'body': '## Revisão independente — 2ª rodada, commit `320815c` (delta `05884c2..320815c`)\n\n**Veredito: APROVAR.**'},
]
RUNS = {'workflow_runs': [{'id': 35745486148, 'name': 'Base técnica Docker', 'status': 'completed',
                           'conclusion': 'success', 'head_sha': MERGE,
                           'html_url': 'https://github.com/demo/repo/actions/runs/35745486148'}]}
ALLOWED = re.compile(r'(issues|pulls|commits|git/trees|contents|actions/runs)(/[^\s?#]*)?(\?[^\s#]*)?')


class FakeGh:
    """Replays recorded shapes; asserts every call is an allowlisted GET."""
    def __init__(self):
        self.calls = []
        self.fix = {'issues/71': {'number': 71, 'title': 'Rodapé fixo', 'body': 'Pendente: E2E e revisão.',
                                  'state': 'open', 'state_reason': None, 'user': {'login': 'delltrak'},
                                  'assignees': [], 'html_url': 'https://github.com/demo/repo/issues/71',
                                  'updated_at': '2026-09-22T13:27:03Z'},
                    'issues/71/comments': [], 'commits': [{'sha': MERGE}],
                    f'git/trees/{MERGE}': {'truncated': False, 'tree': [{'path': 'src/a.ts', 'type': 'blob'}]},
                    'issues/71/timeline': [{'event': 'labeled', 'id': 1, 'created_at': '2026-09-22T13:27:05Z'},
                                           cross_ref(72)],
                    'pulls/72': pull(72), 'pulls/72/reviews': [], 'issues/72/comments': VERDICT_COMMENTS,
                    'actions/runs': RUNS}

    def __call__(self, args, **kwargs):
        assert args[:4] == ['gh', 'api', '--method', 'GET'], args
        resource = args[4].split(f'repos/{REPO}/', 1)[1]
        assert ALLOWED.fullmatch(resource), resource
        self.calls.append(resource)
        path = resource.split('?')[0]
        if path.startswith('actions/runs/'):
            return subprocess.CompletedProcess(args, 0, json.dumps({'jobs': [], 'artifacts': []}), '')
        if path not in self.fix:
            return subprocess.CompletedProcess(args, 1, '', 'HTTP 404')
        return subprocess.CompletedProcess(args, 0, json.dumps(self.fix[path]), '')


def model_result(closure=None, questions=None):
    return {'status': 'awaiting_validation', 'summary': 'Aguardando validação.', 'voice_script': 'Texto.',
            'findings': [{'claim': 'Issue aberta.', 'evidence_ids': ['issue'], 'certainty': 'observed'}],
            'next_steps': [], 'branch_recommendation': 'candidate', 'limitations': [],
            'questions_for_author': questions if questions is not None else
            ['Qual é o PR desta tarefa e onde estão os resultados do E2E?'],
            'closure': closure or {'assessment': 'unclear', 'pull_numbers': [], 'evidence_ids': [], 'reason': ''}}


class Model:
    def __init__(self, result): self.result, self.payload = result, None
    def ask(self, instruction, payload, schema, label):
        if schema != RESULT_SCHEMA:
            return {'paths': [], 'reason': 'x'}
        self.payload = payload
        return copy.deepcopy(self.result)


class LinkedPrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)); self.addCleanup(self.store.db.close)
        self.gh = FakeGh()
        self.config = {'repository': REPO, 'assignee': 'delltrak'}

    def run_triage(self, result=None):
        return triage(self.store, GitHub([REPO], run=self.gh), Model(result or model_result()), self.config, 71)

    def test_merged_refs_pr_suggests_close_with_exact_copy(self):
        result = self.run_triage()['result']
        action = result['issue_action']
        self.assertEqual(action['kind'], 'suggest_close')
        self.assertEqual(action['say']['pt'],
            'O PR #72 já foi mergeado na main em 22/09 (CI verde, revisão aprovada no último commit). '
            'O PR usou Refs #71, que não fecha a issue automaticamente. '
            'Sugiro fechar a #71: https://github.com/demo/repo/issues/71')
        self.assertEqual(action['say']['en'],
            'PR #72 was already merged into main on Sep 22 (green CI, review approved on the latest commit). '
            'The PR used Refs #71, which does not auto-close the issue. '
            'I suggest closing #71: https://github.com/demo/repo/issues/71')
        self.assertNotIn('`', action['say']['pt'] + action['say']['en'])
        self.assertNotIn('APROVAR', action['say']['en'])
        self.assertEqual(result['status'], 'awaiting_release')
        self.assertEqual(result['branch_recommendation'], 'not_needed')
        self.assertEqual(result['questions_for_author'], [])  # never "qual é o PR?"
        self.assertEqual(result['findings'][0]['evidence_ids'], ['pull:72'])
        self.assertEqual(result['evidence']['pull:72']['url'], 'https://github.com/demo/repo/pull/72')

    def test_model_pending_downgrades_but_never_hides_the_pr(self):
        closure = {'assessment': 'pending', 'pull_numbers': [72], 'evidence_ids': ['pull:72'],
                   'reason': 'o E2E completo não aparece no PR'}
        action = self.run_triage(model_result(closure))['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('O PR #72 já foi mergeado', action['say']['pt'])
        self.assertIn('Confira antes de fechar a #71', action['say']['pt'])

    def test_model_cannot_cite_unknown_pull(self):
        from watson.analysis import sanitize_closure
        clean = sanitize_closure({'assessment': 'delivered', 'pull_numbers': [99], 'evidence_ids': ['pull:99'],
                                  'reason': 'x'}, {'issue': {}, 'pull:72': {}})
        self.assertEqual(clean, {'assessment': 'unclear', 'pull_numbers': [], 'evidence_ids': [], 'reason': 'x'})

    def test_pending_without_pull_numbers_downgrades_instead_of_aborting(self):
        closure = {'assessment': 'pending', 'pull_numbers': [], 'evidence_ids': ['issue'],
                   'reason': 'A issue ainda lista E2E completo pendente, que nenhum PR cobre.'}
        action = self.run_triage(model_result(closure))['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('E2E completo pendente', action['say']['pt'])

    def test_later_multi_issue_pr_blocks_close(self):
        later = cross_ref(47, merged_at='2026-09-22T16:00:00Z', body='Refs #71\nRefs #40\nRefs #44')
        self.gh.fix['issues/71/timeline'].append(later)
        self.gh.fix['pulls/47'] = {**pull(47), 'merged_at': '2026-09-22T16:00:00Z'}
        self.gh.fix['pulls/47/reviews'] = []
        self.gh.fix['issues/47/comments'] = []
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('o PR #47, mergeado depois, também cita a #71 sem fechá-la', action['say']['pt'])
        self.assertIn('PR #47, merged later, also cites #71 without closing it', action['say']['en'])

    def test_weak_or_partial_links_never_suggest_close(self):
        for body in ('Part of #71 (first slice).', 'See #71', 'Related to #71', 'Ver #71',
                     'Refs #71 (parte 1 de 3)', 'Parte de #71'):
            self.gh.fix['issues/71/timeline'][1] = cross_ref(72, body=body)
            action = self.run_triage()['result']['issue_action']
            self.assertNotIn(action['kind'], {'suggest_close', 'review_close'}, body)

    def test_link_added_after_merge_or_untrusted_author_needs_review(self):
        late = cross_ref(72, body='Fix typo.\n\nCloses #71')
        late['created_at'] = '2026-09-23T09:00:00Z'  # after the 2026-09-22T15:10:36Z merge
        self.gh.fix['issues/71/timeline'][1] = late
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('depois do merge', action['say']['pt'])
        self.gh.fix['issues/71/timeline'][1] = cross_ref(72, association='CONTRIBUTOR')
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('sem acesso de escrita', action['say']['pt'])

    def test_unknown_revert_state_needs_review(self):
        self.gh.fix['commits'] = [{'sha': 'b' * 40}]
        self.gh.fix[f'git/trees/{"b" * 40}'] = self.gh.fix.pop(f'git/trees/{MERGE}')
        window = [{'sha': MERGE, 'commit': {'message': 'x (#72)'}}]
        busy = [{'sha': f'{i:040x}', 'commit': {'message': f'c{i}'}} for i in range(100)]
        real = self.gh.__call__
        def run(args, **kw):
            if 'commits?sha=' in args[4]:
                return subprocess.CompletedProcess(args, 0, json.dumps(window if 'until=' in args[4] else busy), '')
            return real(args, **kw)
        result = triage(self.store, GitHub([REPO], run=run), Model(model_result()), self.config, 71)['result']
        self.assertEqual(result['issue_action']['kind'], 'review_close')
        self.assertIn('não foi revertido', result['issue_action']['say']['pt'])

    def test_superseded_cancelled_runs_are_not_failures(self):
        from watson.linked import ci_summary
        def run(n, conclusion, wf=1):
            return {'id': n, 'run_number': n, 'workflow_id': wf, 'name': 'Qualidade', 'status': 'completed',
                    'conclusion': conclusion, 'html_url': f'u{n}'}
        self.assertEqual(ci_summary([run(56, 'cancelled'), run(55, 'cancelled'), run(57, 'success')])['conclusion'],
                         'success')
        self.assertEqual(ci_summary([run(1, 'success'), run(2, 'failure')])['conclusion'], 'failure')
        self.assertEqual(ci_summary([run(1, 'cancelled'), run(2, 'failure')])['conclusion'], 'failure')
        self.assertEqual(ci_summary([run(1, 'cancelled')])['conclusion'], 'none')
        self.assertEqual(ci_summary([run(1, 'success', 1), run(2, 'failure', 2)])['conclusion'], 'failure')

    def test_patterns_are_linear_on_adversarial_bodies(self):
        import time
        from watson.linked import UNCHECKED, VERDICT
        started = time.monotonic()
        targets('#71' + '\n' * 65533)
        link_kind(REPO, 71, REPO, 'x', '#71' + '\n' * 65533)
        UNCHECKED.findall('\n' * 65536)
        VERDICT.search('verdict' + ' ' * 5000)
        verdicts([{'author_association': 'OWNER', 'body': 'veredito' + ' ' * 5000, 'user': {'login': 'x'},
                   'html_url': 'u'}], HEAD, 'x')
        self.assertLess(time.monotonic() - started, 2.0)

    def test_english_run_limitations_are_english_only(self):
        self.gh.fix['issues/71/timeline'][1] = cross_ref(7, repo='lemonity-org/azud')
        self.gh.fix['issues/71/timeline'].append({'event': 'connected', 'created_at': '2026-09-22T13:30:00Z'})
        result = triage(self.store, GitHub([REPO], run=self.gh), Model(model_result()), self.config, 71,
                        language='en')['result']
        for note in result['limitations']:
            self.assertNotIn('---', note)
            for marker in ('Não ', 'Há ', 'Somente', 'Arquivo', 'fora do escopo'):
                self.assertNotIn(marker, note)
        self.assertIn('1 PR(s) from other repositories cite the issue; out of scope.', result['limitations'])

    def test_negated_mention_is_not_a_close_candidate(self):
        self.assertEqual(link_kind(REPO, 62, REPO, 'x', PR_BODY), 'mention_negated')
        self.assertEqual(link_kind(REPO, 39, REPO, 'x', PR_BODY), 'mention_negated')
        self.assertEqual(link_kind(REPO, 71, REPO, 'x', PR_BODY), 'refs')
        self.assertEqual(link_kind(REPO, 7, REPO, 'x', 'Refs #71'), 'mention')
        for body in ['Closes #71', 'fixes: #71', 'Resolves demo/repo#71', 'close https://github.com/demo/repo/issues/71']:
            self.assertEqual(link_kind(REPO, 71, REPO, 'x', body), 'closes', body)
        self.assertEqual(link_kind(REPO, 71, 'demo/other', 'x', 'Closes #71'), 'mention')  # other repo's #71
        self.assertEqual(link_kind(REPO, 71, REPO, 'x', 'Closes #710'), 'mention')

    def test_multi_target_refs_is_partial_not_close(self):
        self.gh.fix['issues/71/timeline'][1] = cross_ref(72, body='Refs #71\nRefs #4\nRefs #5')
        self.assertEqual(targets('Refs #71\nRefs #4\nRefs #5'), [4, 5, 71])
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'merged_partial')
        self.assertFalse(action['lead'])

    def test_unchecked_tasks_block_suggest_close(self):
        self.gh.fix['issues/71']['body'] = '- [ ] E2E\n- [x] Lint\n- [ ] Revisão'
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('2 itens sem marcar', action['say']['pt'])

    def test_open_trusted_pr_blocks_duplicate_draft(self):
        self.gh.fix['issues/71/timeline'][1] = cross_ref(72, merged_at=None, state='open', draft=True)
        self.gh.fix['pulls/72'] = pull(72, merged=False)
        result = self.run_triage()['result']
        self.assertEqual(result['issue_action']['kind'], 'pr_open')
        self.assertEqual(result['branch_recommendation'], 'not_needed')
        self.assertTrue(result['issue_action']['say']['pt'].startswith('Já existe o PR #72 em rascunho para a #71'))

    def test_untrusted_open_pr_does_not_block(self):
        self.gh.fix['issues/71/timeline'][1] = cross_ref(72, merged_at=None, state='open', association='NONE')
        self.gh.fix['pulls/72'] = pull(72, merged=False)
        self.assertEqual(self.run_triage()['result']['issue_action']['kind'], 'none')
        self.assertNotIn('pulls/72', self.gh.calls)  # not even hydrated

    def test_foreign_repo_and_issue_cross_refs_ignored(self):
        self.gh.fix['issues/71/timeline'][1] = cross_ref(7, repo='lemonity-org/azud')
        self.gh.fix['issues/71/timeline'].append(cross_ref(63, is_pr=False))
        result = self.run_triage()['result']
        self.assertEqual(result['issue_action']['kind'], 'none')
        self.assertIn('1 PR(s) de outros repositórios citam a issue; fora do escopo.', result['limitations'])

    def test_timeline_failure_is_unknown_not_absent(self):
        del self.gh.fix['issues/71/timeline']
        result = self.run_triage()['result']
        self.assertEqual(result['issue_action']['kind'], 'none')
        self.assertTrue(any('Não consegui verificar PRs ligados' in x for x in result['limitations']))

    def test_merge_found_when_committer_date_precedes_merged_at(self):
        self.gh.fix['commits'] = [{'sha': 'b' * 40}]  # HEAD moved past the merge
        self.gh.fix[f'git/trees/{"b" * 40}'] = self.gh.fix.pop(f'git/trees/{MERGE}')
        window = [{'sha': MERGE, 'commit': {'message': 'x (#72)', 'committer': {'date': '2026-09-22T15:10:35Z'}}}]
        real = self.gh.__call__
        def run(args, **kw):
            if 'commits?sha=' in args[4]:
                self.gh.calls.append(args[4].split(f'repos/{REPO}/', 1)[1])
                return subprocess.CompletedProcess(args, 0, json.dumps(window), '')
            return real(args, **kw)
        result = triage(self.store, GitHub([REPO], run=run), Model(model_result()), self.config, 71)['result']
        self.assertEqual(result['issue_action']['kind'], 'suggest_close')
        self.assertTrue(any('since=2026-09-22T14:10:36Z&until=2026-09-22T16:10:36Z' in c for c in self.gh.calls))

    def test_reverted_merge_is_not_a_close_candidate(self):
        self.gh.fix['commits'] = [{'sha': 'b' * 40}]
        self.gh.fix[f'git/trees/{"b" * 40}'] = self.gh.fix.pop(f'git/trees/{MERGE}')
        history = [{'sha': 'b' * 40, 'commit': {'message': f'Revert "x"\n\nThis reverts commit {MERGE}.'}},
                   {'sha': MERGE, 'commit': {'message': 'x (#72)'}}]
        real = self.gh.__call__
        def run(args, **kw):
            if 'commits?sha=' in args[4]:
                return subprocess.CompletedProcess(args, 0, json.dumps(history), '')
            return real(args, **kw)
        result = triage(self.store, GitHub([REPO], run=run), Model(model_result()), self.config, 71)['result']
        self.assertNotIn(result['issue_action']['kind'], {'suggest_close', 'review_close'})

    def test_reopened_after_merge_blocks_close(self):
        self.gh.fix['issues/71/timeline'].append({'event': 'reopened', 'created_at': '2026-09-23T10:00:00Z'})
        action = self.run_triage()['result']['issue_action']
        self.assertEqual(action['kind'], 'review_close')
        self.assertIn('reaberta em 23/09', action['say']['pt'])

    def test_ci_failure_on_merge_downgrades(self):
        self.gh.fix['actions/runs'] = {'workflow_runs': [{**RUNS['workflow_runs'][0], 'conclusion': 'failure'}]}
        self.assertEqual(self.run_triage()['result']['issue_action']['kind'], 'review_close')

    def test_review_verdict_bound_to_head_sha_and_trusted_author(self):
        found = verdicts(VERDICT_COMMENTS, HEAD, 'delltrak')
        self.assertEqual([(v['verdict'], v['on_head']) for v in found],
                         [('APROVAR COM RESSALVAS', False), ('APROVAR', True)])
        drive_by = [{**VERDICT_COMMENTS[1], 'author_association': 'NONE'}]
        self.assertEqual(verdicts(drive_by, HEAD, 'delltrak'), [])

    def test_pr_state_change_invalidates_cache_and_version_bump(self):
        first = self.run_triage()
        self.assertTrue(self.run_triage()['cached'])
        self.gh.fix['issues/71/timeline'][1] = cross_ref(72, merged_at=None, state='open')
        self.gh.fix['pulls/72'] = pull(72, merged=False)
        second = self.run_triage()
        self.assertFalse(second['cached'])
        self.assertNotEqual(first['run_id'], second['run_id'])

    def test_closed_issue_gets_no_suggestion(self):
        self.gh.fix['issues/71']['state'] = 'closed'
        self.assertEqual(self.run_triage()['result']['issue_action']['kind'], 'none')


class SpeakFirstTests(unittest.TestCase):
    def test_investigate_surfaces_speak_first_in_requested_language(self):
        from unittest.mock import patch
        from watson.core import private_json
        from watson.mcp import dispatch
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        home = Path(temp.name); Store(home).db.close()
        private_json(home / 'config.json', {'repository': REPO, 'assignee': 'o', 'related_repositories': []})
        say_map = {'pt': 'O PR #72 já foi mergeado. Sugiro fechar a #71: url', 'en': 'PR #72 was merged. I suggest closing #71: url'}
        fake = {'run_id': 1, 'cached': False, 'result': {'issue_action': {'kind': 'suggest_close', 'lead': True, 'say': say_map}}}
        for language, expected in [('pt', say_map['pt']), ('en', say_map['en']), (None, say_map['pt'])]:
            args = {'issue': '#71', **({'language': language} if language else {})}
            with patch('watson.mcp.require_github'), patch('watson.mcp.require_codex'), \
                 patch('watson.mcp.triage', return_value=copy.deepcopy(fake)), patch('watson.mcp.GitHub'), patch('watson.mcp.Codex'):
                response = dispatch(home, {'method': 'tools/call', 'params': {'name': 'watson_investigate', 'arguments': args}})
            payload = json.loads(response['content'][0]['text'])
            self.assertEqual(payload['speak_first'], expected)
            self.assertIn('never closes issues', payload['instruction'])
        fake['result']['issue_action'] = {'kind': 'merged_partial', 'lead': False, 'say': {'pt': '', 'en': ''}}
        with patch('watson.mcp.require_github'), patch('watson.mcp.require_codex'), \
             patch('watson.mcp.triage', return_value=fake), patch('watson.mcp.GitHub'), patch('watson.mcp.Codex'):
            response = dispatch(home, {'method': 'tools/call', 'params': {'name': 'watson_investigate', 'arguments': {'number': 71}}})
        self.assertNotIn('speak_first', json.loads(response['content'][0]['text']))


if __name__ == '__main__':
    unittest.main()
