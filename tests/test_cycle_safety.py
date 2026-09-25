import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from watson.cli import main, sync
from watson.core import Store, WatsonError, private_json
from watson.workflow import NOTICE, cycle
from test_watson import FakeGitHub, FakeModel, CONFIG


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


class CycleSafetyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        private_json(self.home / 'config.json', {**CONFIG, 'notify_owner': True})
        self.owner = Owner()
        patcher = patch('watson.workflow.Plow'); self.addCleanup(patcher.stop)
        patcher.start().from_config.return_value = self.owner

    def cli(self, *args):
        with redirect_stdout(io.StringIO()): return main(['--home', str(self.home), *args])

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

    def test_owner_commands_do_not_wait_for_a_running_pass(self):
        store = Store(self.home); self.addCleanup(store.db.close)
        with store.lock():
            for command in (['track', '124'], ['untrack', '124'], ['status']):
                self.assertEqual(self.cli(*command), 0, command)

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
        self.due_now(); self.run_cycle(github)  # the third: already told
        self.assertEqual(len(self.owner.sent), told + 1)

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

    def test_a_failing_triage_is_not_paid_for_every_pass(self):
        class Broken(FakeModel):
            def ask(self, *args):
                self.calls += 1; raise WatsonError('Afirmação sem referência válida; triagem não será enviada.')
        model = Broken(); github = FakeGitHub()
        store = Store(self.home); store.track('demo/repo', 7, explicit=True); store.db.close()
        for _ in range(3): self.run_cycle(github, model)
        self.assertEqual(model.calls, 1)


if __name__ == '__main__':
    unittest.main()
