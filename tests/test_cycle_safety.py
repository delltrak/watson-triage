import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from watson.cli import main, sync
from watson.core import Store, WatsonError, private_json
from watson.github import GitHub, GitHubError
from watson.workflow import NOTICE, cycle
from test_watson import FakeGitHub, FakeModel, CONFIG, RESULT


class Owner:
    """A Plow whose owner chat records what the cycle told it."""
    def __init__(self): self.sent = []
    def owner_chat(self): return 'chat'
    def send(self, chat, body, media=None):
        self.sent.append(body); return {'message_uid': f'm{len(self.sent)}', 'chat_uid': chat}


class Numbers(FakeGitHub):
    """Issue 40 is fine; 5, 6 and 7 always fail, as a pull request number does."""
    error = 'O primeiro MVP acompanha issues, não PRs como assunto principal.'
    def issue(self, repo, number):
        if number in (5, 6, 7): raise WatsonError(self.error)
        return {**copy.deepcopy(self.item), 'number': number}


class Refused(FakeGitHub):
    """GitHub refuses the assigned-issues read, as gh reports it; issue reads are counted."""
    def __init__(self, status):
        super().__init__(); self.status, self.reads = status, []
    def assigned(self, repo, assignee):
        raise GitHubError(f'Não foi possível ler {repo}/issues no GitHub (HTTP {self.status}).', self.status)
    def issue(self, repo, number):
        self.reads.append(number); return super().issue(repo, number)


class CycleSafetyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        private_json(self.home / 'config.json', {**CONFIG, 'notify_owner': True})
        self.owner = Owner()
        patcher = patch('watson.workflow.Plow'); self.addCleanup(patcher.stop)
        patcher.start().from_config.return_value = self.owner
        patcher = patch('watson.workflow.read_status', return_value={}); self.addCleanup(patcher.stop)  # root's, off the image
        self.github_status = patcher.start()

    def cli(self, *args):
        with redirect_stdout(io.StringIO()): return main(['--home', str(self.home), *args])

    def status(self):
        out = io.StringIO()
        with redirect_stdout(out): self.assertEqual(main(['--home', str(self.home), 'status']), 0)
        return json.loads(out.getvalue())

    def row(self, number):
        store = Store(self.home)
        try: return dict(store.db.execute('SELECT * FROM issues WHERE number=?', (number,)).fetchone())
        finally: store.db.close()

    def run_cycle(self, github, model=None):
        return cycle(self.home, model=model or FakeModel(), github=github)

    def due_now(self):
        store = Store(self.home)
        store.db.execute("UPDATE issues SET retry_at='2000-01-01' WHERE retry_at IS NOT NULL"); store.db.commit(); store.db.close()

    def test_an_issue_the_owner_named_is_followed_whoever_it_is_assigned_to(self):
        github = FakeGitHub(); github.item['assignees'] = ['dev']; github.items = []
        self.run_cycle(github)  # baseline
        self.assertEqual(self.cli('track', '7'), 0)
        outcome = self.run_cycle(github)
        self.assertEqual([p['number'] for p in outcome['processed']], [7])
        self.assertEqual(self.row(7)['tracked'], 1)

    def test_an_assignment_driven_track_still_stops_when_unassigned_and_says_so(self):
        github = FakeGitHub(); github.item['assignees'] = ['dev']; github.items = []
        store = Store(self.home); store.track('demo/repo', 7); store.db.close()  # as sync does
        outcome = self.run_cycle(github)
        self.assertEqual(outcome['skipped'], [{'number': 7, 'reason': 'no longer assigned to owner'}])
        self.assertEqual(self.row(7)['tracked'], 0)

    def test_untrack_clears_explicit_and_sync_never_sets_it(self):
        github = FakeGitHub(); assigned, github.items = github.items, []
        store = Store(self.home); self.addCleanup(store.db.close)
        sync(store, github, CONFIG)  # baseline, nothing assigned yet
        self.cli('track', '7'); self.assertEqual(self.row(7)['explicit'], 1)
        self.cli('untrack', '7'); self.assertEqual(self.row(7)['explicit'], 0)
        github.items = assigned
        self.assertEqual(sync(store, github, CONFIG)['newly_tracked'], [7])
        self.assertEqual((self.row(7)['tracked'], self.row(7)['explicit']), (1, 0))
        github.items = []; sync(store, github, CONFIG)  # handed to someone else
        self.cli('track', '7'); github.items = assigned; sync(store, github, CONFIG)  # named, then handed back
        self.assertEqual(self.row(7)['explicit'], 1, 'sync cleared the owner-named flag')

    def test_owner_commands_do_not_wait_for_a_running_pass(self):
        store = Store(self.home); self.addCleanup(store.db.close)
        run_id, _ = store.begin('demo/repo', 124, 'fingerprint'); store.finish(run_id, RESULT)  # already reported
        with store.lock():
            for command in (['track', '124'], ['untrack', '124'], ['status'], ['config', '--assignee', 'pm'],
                            ['show', str(run_id), '--format', 'json']):
                self.assertEqual(self.cli(*command), 0, command)

    def pass_where_the_owner_speaks_about_7(self, command, assignees):
        """#3 and #7 are selected together; while the pass is still on #3, the owner runs `command 7`."""
        owner_speaks = lambda: self.assertEqual(self.cli(command, '7'), 0)
        class MidPass(FakeGitHub):
            said = False
            def issue(self, repo, number):
                if number == 3 and not self.said: self.said = True; owner_speaks()
                return {**copy.deepcopy(self.item), 'number': number, 'assignees': assignees if number == 7 else ['owner']}
        store = Store(self.home); store.track('demo/repo', 3); store.track('demo/repo', 7); store.db.close()  # as sync does
        github = MidPass(); github.items = []
        processed = [p['number'] for p in self.run_cycle(github)['processed']]
        return processed, [m for m in self.owner.sent if m.startswith('Watson · #7')]

    def test_a_track_made_during_a_pass_is_not_undone_by_it(self):
        processed, told = self.pass_where_the_owner_speaks_about_7('track', ['dev'])  # "keep following #7"
        self.assertEqual((processed, len(told)), ([3, 7], 1))
        self.assertEqual((self.row(7)['tracked'], self.row(7)['explicit']), (1, 1))

    def test_an_untrack_made_during_a_pass_is_honoured_by_it(self):
        self.assertEqual(self.pass_where_the_owner_speaks_about_7('untrack', ['owner']), ([3], []))

    def test_numbers_that_always_fail_neither_starve_the_queue_nor_nag(self):
        github = Numbers()
        for n in (5, 6, 7): self.cli('track', str(n))
        store = Store(self.home); store.track('demo/repo', 40); store.db.close()  # as sync does
        first = self.run_cycle(github)
        self.assertEqual([e['number'] for e in first['errors']], [5, 6, 7])
        second = self.run_cycle(github)
        self.assertEqual([p['number'] for p in second['processed']], [40], 'the failing numbers held every slot again')
        self.assertFalse([m for m in self.owner.sent if '#5' in m], 'one failure is not worth a message')
        self.due_now(); told = len(self.owner.sent)
        self.run_cycle(github)  # the second failure in a row
        self.assertEqual(self.owner.sent[told:], [NOTICE['en']['stuck'].format(numbers='#5, #6, #7')])
        self.assertNotIn(github.error, self.owner.sent[-1])
        self.cli('untrack', '6'); self.cli('untrack', '7')  # #5 alone fails a third time: already told
        self.due_now(); self.run_cycle(github)
        self.assertEqual(len(self.owner.sent), told + 1)
        for _ in range(2):  # a success ends the streak, and the next one is told again
            store = Store(self.home); store.checked('demo/repo', 5); store.db.close()
            for _ in range(2): self.due_now(); self.run_cycle(github)
        self.assertEqual(self.owner.sent[told + 1:], [NOTICE['en']['stuck'].format(numbers='#5')] * 2)

    def test_why_a_number_is_stuck_outlives_the_pass_that_found_it(self):
        github = Numbers(); self.cli('track', '5')
        self.run_cycle(github); self.due_now(); self.run_cycle(github)  # stuck: the owner is told to ask why
        self.run_cycle(github)  # the next pass, with #5 waiting out its backoff
        status = self.status()
        self.assertEqual(status['last_cycle']['errors'], [])
        self.assertEqual([i['last_error'] for i in status['issues'] if i['number'] == 5], [github.error])
        store = Store(self.home); store.checked('demo/repo', 5); store.db.close()
        self.assertIsNone(self.row(5)['last_error'])

    def test_the_backoff_doubles_and_is_capped_at_a_day(self):
        store = Store(self.home); self.addCleanup(store.db.close); store.track('demo/repo', 5)
        waits = []
        for _ in range(10):
            before = datetime.now(timezone.utc); store.failed('demo/repo', 5)
            at = datetime.fromisoformat(self.row(5)['retry_at'])
            waits.append(round((at - before).total_seconds() / 60))
        self.assertEqual(waits, [10, 20, 40, 80, 160, 320, 640, 1280, 1440, 1440])
        store.checked('demo/repo', 5)
        self.assertEqual((self.row(5)['failures'], self.row(5)['retry_at']), (0, None))
        store.failed('demo/repo', 5); store.track('demo/repo', 5)  # so does a (re)track
        self.assertEqual((self.row(5)['failures'], self.row(5)['retry_at']), (0, None))

    def test_a_failing_triage_is_not_paid_for_every_pass(self):
        class Broken(FakeModel):
            def ask(self, *args):
                self.calls += 1; raise WatsonError('Afirmação sem referência válida; triagem não será enviada.')
        model = Broken(); github = FakeGitHub()
        store = Store(self.home); store.track('demo/repo', 7, explicit=True); store.db.close()
        for _ in range(3): self.run_cycle(github, model)
        self.assertEqual(model.calls, 1)

    def test_a_refused_sync_is_reported_once_and_skips_the_issues(self):
        github = Refused(404); self.cli('track', '40')
        error = {'sync': 'Não foi possível ler demo/repo/issues no GitHub (HTTP 404).'}
        for _ in range(3): self.assertEqual(self.run_cycle(github)['errors'], [error])
        self.assertEqual(github.reads, [], 'issues were read although the repository was refused')
        self.assertEqual(self.owner.sent, [NOTICE['en']['refused'][404].format(repo='demo/repo')])
        self.assertEqual(self.status()['last_cycle']['errors'], [error])

    def test_a_quiet_install_is_told_nothing(self):
        private_json(self.home / 'config.json', {**CONFIG, 'notify_owner': False})
        self.run_cycle(Refused(404))
        self.assertEqual(self.owner.sent, [])

    def test_the_same_refusal_is_reported_again_after_a_good_sync(self):
        for github in (Refused(401), FakeGitHub(), Refused(401), Refused(401)): self.run_cycle(github)
        refused = NOTICE['en']['refused'][401].format(repo='demo/repo')
        self.assertEqual([m for m in self.owner.sent if m == refused], [refused, refused])

    def test_github_the_owner_disconnected_is_not_reported_as_refused(self):
        # Statuses as root publishes them. The owner's disconnect stands, by_owner
        # with it, through a reconnect they abandon or deny.
        for status in ({'state': 'disconnected', 'detail': 'by_owner', 'by_owner': True},
                       {'state': 'expired', 'by_owner': True},
                       {'state': 'denied', 'detail': 'access_denied', 'by_owner': True}):
            self.github_status.return_value = status
            for _ in range(2): self.assertTrue(self.run_cycle(Refused(401))['errors'])
        self.assertEqual(self.owner.sent, [])
        # A token root dropped itself is still news: the first refused pass sees 'reconnect', later ticks 'disconnected'.
        refused = NOTICE['en']['refused'][401].format(repo='demo/repo')
        self.github_status.return_value = {'state': 'reconnect', 'detail': 'github_refused'}
        self.run_cycle(Refused(401))
        self.assertEqual([m for m in self.owner.sent if m == refused], [refused])
        self.run_cycle(FakeGitHub())  # a good sync: the next refusal is news again
        self.github_status.return_value = {'state': 'disconnected'}
        self.run_cycle(Refused(401))
        self.assertEqual([m for m in self.owner.sent if m == refused], [refused, refused])

    def test_a_transient_sync_failure_is_not_a_message(self):
        class TimedOut(FakeGitHub):
            def assigned(self, repo, assignee): raise subprocess.TimeoutExpired(['gh', 'api'], 90)
        for name, github in (('502', Refused(502)), ('timeout', TimedOut())):
            with self.subTest(name):
                self.assertTrue(self.run_cycle(github)['errors'])
                self.assertEqual(self.owner.sent, [])

    def test_setup_is_confirmed_once_with_what_was_found(self):
        github = FakeGitHub(); github.items = []
        self.run_cycle(github); self.run_cycle(github)
        self.assertEqual(self.owner.sent, [NOTICE['en']['set_up'].format(repo='demo/repo', assignee='owner', count=0)
                                           + NOTICE['en']['no_issues'].format(assignee='owner')])

    def test_config_changes_one_setting_and_rebaselines_instead_of_tracking_a_backlog(self):
        github = FakeGitHub(); backlog, github.items = github.items, []
        self.run_cycle(github)  # the baseline for owner, who has nothing assigned
        self.assertEqual(self.cli('config', '--assignee', 'pm'), 0)
        config = {**CONFIG, 'notify_owner': True, 'assignee': 'pm'}
        self.assertEqual(json.loads((self.home / 'config.json').read_text()), config)
        github.items = backlog  # pm's backlog
        outcome = self.run_cycle(github)
        self.assertEqual((outcome['sync']['initial_baseline'], outcome['sync']['newly_tracked']), (True, []))
        self.assertEqual(self.owner.sent[-1], NOTICE['en']['set_up'].format(repo='demo/repo', assignee='pm', count=1))
        for bad in (['--assignee', 'not a login'], ['--repo', 'nope'], []):
            self.assertEqual(self.cli('config', *bad), 1, bad)
        self.assertEqual(json.loads((self.home / 'config.json').read_text()), config)
        err = io.StringIO()
        with redirect_stderr(err): self.assertEqual(self.cli('init', '--repo', 'demo/other', '--assignee', 'dev'), 1)
        self.assertIn('watson config', err.getvalue())

    def test_a_baseline_recorded_before_the_upgrade_still_counts(self):
        private_json(self.home / 'baseline.json', {'repository': 'demo/repo', 'assignee': 'owner'})
        self.assertEqual(self.run_cycle(FakeGitHub())['sync']['newly_tracked'], [7])

    def test_github_errors_carry_the_status_and_nothing_else_gh_printed(self):
        # gh exits 4, printing no status, when it has no credential at all: root dropped one GitHub refused.
        for code, out, err in ((1, '{"message":"Bad credentials"}', 'gh: Bad credentials (HTTP 401)\n'),
                               (4, '', 'To get started with GitHub CLI, please run:  gh auth login\n'
                                       'Alternatively, populate the GH_TOKEN environment variable with a GitHub API '
                                       'authentication token.\n')):
            with self.subTest(exit=code):
                run = lambda args, **kwargs: subprocess.CompletedProcess(args, code, out, err)
                with self.assertRaises(GitHubError) as caught:
                    GitHub(['demo/repo'], run=run).get('demo/repo', 'issues?state=open')
                self.assertEqual(caught.exception.status, 401)
                self.assertEqual(str(caught.exception), 'Não foi possível ler demo/repo/issues no GitHub (HTTP 401).')


if __name__ == '__main__':
    unittest.main()
