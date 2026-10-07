import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watson.analysis import STATIC_LIMITATION, triage
from watson.cli import main
from watson.core import Store
from watson.workflow import compose, notify
from test_watson import CONFIG, ISSUE, FakeGitHub, FakeModel


class Recording(FakeModel):
    def __init__(self):
        super().__init__(); self.instructions = []
    def ask(self, instruction, payload, schema, label):
        self.instructions.append(instruction)
        if label.startswith('comment-'):
            return {'language': 'en', 'body': 'Which version?', 'owner_summary': 's'}
        return super().ask(instruction, payload, schema, label)


class OwnerLanguageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--home', str(self.home), *argv])
        return code, out.getvalue(), err.getvalue()

    def init(self, *extra):
        return self.run_cli('init', '--repo', 'demo/repo', '--assignee', 'owner', *extra)

    def config(self):
        return json.loads((self.home / 'config.json').read_text())

    def test_init_saves_the_language_and_defaults_to_english(self):
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.config()['language'], 'en')
        (self.home / 'config.json').unlink()
        self.assertEqual(self.init('--language', 'pt')[0], 0)
        self.assertEqual(self.config()['language'], 'pt')
        with self.assertRaises(SystemExit):
            self.run_cli('config', '--language', 'es')

    def test_status_and_config_answer_before_init_and_while_a_pass_holds_the_lock(self):
        with patch('watson.cli.read_status', dict):
            code, out, _ = self.run_cli('status')
            self.assertEqual((code, json.loads(out)), (0, {'configured': False, 'github': {'state': 'unknown'}}))
            self.assertFalse((self.home / 'memory.sqlite').exists(), 'a status before init created the database')
            code, _, err = self.run_cli('config', '--language', 'pt')
            self.assertEqual(code, 1, 'no config to change yet')
            self.assertIn('run watson init first', err)
            self.init(); before = self.config()
            store = Store(self.home); self.addCleanup(store.db.close)
            with store.lock():
                self.assertEqual(self.run_cli('config', '--language', 'pt')[0], 0)
                code, out, _ = self.run_cli('status')
        self.assertEqual(self.config(), {**before, 'language': 'pt'})
        self.assertEqual((code, json.loads(out)['configured'], json.loads(out)['config']['language']), (0, True, 'pt'))

    def test_errors_the_setup_skill_can_hit_are_english(self):
        self.assertIn('run watson init first', self.run_cli('track', '5')[2])
        self.assertIn('Invalid repository', self.init('--repo', 'nope')[2])
        self.init()
        self.assertIn('Invalid issue number', self.run_cli('track', '0')[2])
        store = Store(self.home); self.addCleanup(store.db.close)
        with store.lock():
            self.assertIn('Another Watson pass is running', self.run_cli('sync')[2])

    def test_the_notice_is_worded_in_the_owner_language(self):
        store = Store(self.home); self.addCleanup(store.db.close)
        sent = []
        class Plow:
            def send(self, chat, body, media): sent.append(body); return {'uid': 'r'}
        validation = {'status': 'failed', 'steps': [{'status': 'failed', 'expected': 'ok', 'actual': 'boom'}]}
        # A config from before languages, or with one Watson does not speak, is English.
        for run_id, language in enumerate((None, 'fr', 'pt'), 1):
            config = {'notify_owner': True, **({'language': language} if language else {})}
            notify(store, (Plow(), 'chat'), config, run_id, ISSUE, {'summary': 's'}, 'reproduced', validation)
        for english in sent[:2]:
            self.assertIn('Problem reproduced in the test', english)
            self.assertIn('Expected: ok\nObserved: boom', english)
        self.assertIn('Problema reproduzido no teste', sent[2])
        self.assertIn('Esperado: ok\nObservado: boom', sent[2])

    def test_triage_writes_in_the_owner_language_and_caches_per_language(self):
        store = Store(self.home); self.addCleanup(store.db.close)
        model, github = Recording(), FakeGitHub()
        first = triage(store, github, model, CONFIG, 7)
        self.assertTrue(model.instructions[-1].endswith('in English.'))
        self.assertNotIn('português', model.instructions[-1])
        self.assertIn(STATIC_LIMITATION['en'], first['result']['limitations'])
        second = triage(store, github, model, dict(CONFIG, language='pt'), 7)
        self.assertFalse(second['cached'], 'a language change reused the answer written in the other one')
        self.assertTrue(model.instructions[-1].endswith('in Brazilian Portuguese.'))
        self.assertIn(STATIC_LIMITATION['pt'], second['result']['limitations'])

    def test_the_owner_summary_follows_the_owner_language(self):
        model = Recording()
        for language, name in (('en', 'English'), ('pt', 'Brazilian Portuguese')):
            compose(model, ISSUE, {'summary': 's'}, 'waiting_info', None, 'channel', None, language)
            self.assertIn(f'owner_summary in {name} ', model.instructions[-1])


if __name__ == '__main__':
    unittest.main()
