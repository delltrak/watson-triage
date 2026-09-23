import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watson import onboarding
from watson.capabilities import short_greeting
from watson.core import Store
from watson.greeting import classify_help, owner_shortcut
from watson.issue_ref import parse_issue_ref, repo_note, resolve_issue_target

ROOT = Path(__file__).resolve().parents[1]
GH = {'connected': True, 'reason': 'token_env', 'login': 'delltrak'}


def report(codex=True, claude=False, gh=True):
    return {'github': {**GH, 'connected': gh}, 'codex': {'connected': codex}, 'claude': {'connected': claude},
            'speak_this': 'FULL CHECKLIST'}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_full_once_then_short(self):
        r = report()
        self.assertEqual(onboarding.decide(onboarding.load(self.home), r, 'owner'), 'full')
        onboarding.mark_shown(self.home, r, 'owner')
        self.assertEqual(oct((self.home / 'owner.json').stat().st_mode & 0o777), '0o600')
        self.assertEqual(onboarding.decide(onboarding.load(self.home), r, 'owner'), 'short')
        self.assertEqual(onboarding.decide(onboarding.load(self.home), report(claude=True), 'owner'), 'full')
        self.assertEqual(onboarding.decide(onboarding.load(self.home), r, 'someone-else'), 'full')
        self.assertEqual(onboarding.decide(onboarding.load(self.home), report(gh=False), 'owner'), 'full')
        self.assertEqual(onboarding.decide(onboarding.load(self.home), report(codex=False), 'owner'), 'full')

    def test_bad_state_means_full(self):
        for content in ('not json', '[]', ''):
            (self.home / 'owner.json').write_text(content)
            self.assertEqual(onboarding.decide(onboarding.load(self.home), report(), 'owner'), 'full', content)
        (self.home / 'owner.json').unlink()
        target = self.home / 'elsewhere.json'
        target.write_text(json.dumps({'owner_uid': 'owner', 'shown_fingerprint': onboarding.fingerprint(report())}))
        (self.home / 'owner.json').symlink_to(target)
        self.assertEqual(onboarding.decide(onboarding.load(self.home), report(), 'owner'), 'full')

    def test_short_copy(self):
        pt = short_greeting(report(), 'pt')
        self.assertIn('Manda o número ou o link da issue', pt)
        self.assertIn('Time Claude segue sem login; manda "conecta o Claude"', pt)
        en = short_greeting(report(claude=True), 'en')
        self.assertEqual(en, 'Hey! 👋 Send me the issue number or link and I will investigate.')
        for text in (pt, en):
            self.assertNotIn('`', text)
            self.assertNotIn('1. **GitHub**', text)

    def test_owner_greeting_flow_through_the_hook_path(self):
        with patch('watson.greeting.capabilities_report', return_value=report()):
            first = owner_shortcut('Oi', self.home, owner_uid='owner', remember=True)
            second = owner_shortcut('oi', self.home, owner_uid='owner', remember=True)
            en = owner_shortcut('hey', self.home, owner_uid='owner', remember=True)
            not_owner = owner_shortcut('oi', self.home)  # no owner → always the full copy
        self.assertEqual(first, 'FULL CHECKLIST')
        self.assertTrue(second.startswith('Oi! 👋'))
        self.assertTrue(en.startswith('Hey! 👋'))
        self.assertEqual(not_owner, 'FULL CHECKLIST')


class HelpIntentTests(unittest.TestCase):
    def test_natural_language_help(self):
        for text, lang in (('ajuda', 'pt'), ('Ajuda por favor', 'pt'), ('o que você faz?', 'pt'),
                           ('quais comandos?', 'pt'), ('quem é você?', 'pt'), ('help', 'en'),
                           ('what can you do?', 'en'), ('who are you?', 'en'), ('watson, help', 'en')):
            self.assertEqual(classify_help(text), lang, text)
        # Follow-ups that need the conversation go to the LLM, not the static menu.
        for text in ('me ajuda com a issue 71', 'preciso de ajuda com o deploy', 'help me fix #12', 'oi',
                     'como funciona?', 'me ajuda', 'preciso de ajuda', 'how does this work?'):
            self.assertIsNone(classify_help(text), text)

    def test_one_word_help_follows_the_owner_language(self):
        self.assertEqual(classify_help('help', 'pt'), 'pt')
        self.assertEqual(classify_help('menu', 'en'), 'en')
        self.assertEqual(classify_help('what can you do?', 'pt'), 'en')  # clearly English phrase
        with tempfile.TemporaryDirectory() as folder:
            from watson.chat_help import help_text
            from watson.language import remember_language
            remember_language(Path(folder), 'pt')
            self.assertEqual(owner_shortcut('help', Path(folder)), help_text('pt'))

    def test_owner_shortcut_answers_help(self):
        from watson.chat_help import help_text
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(owner_shortcut('o que você faz?', Path(folder)), help_text('pt'))
            self.assertEqual(owner_shortcut('what can you do?', Path(folder)), help_text('en'))


class RecentRepoTests(unittest.TestCase):
    def test_owner_repo_number_and_resolution_order(self):
        self.assertEqual(parse_issue_ref('delltrak/lab#7'), ('delltrak/lab', 7))
        config = {'repository': 'demo/default'}
        self.assertEqual(resolve_issue_target('#7', config, 'delltrak/lab'), ('delltrak/lab', 7, 'recent'))
        self.assertEqual(resolve_issue_target('#7', config), ('demo/default', 7, 'default'))
        self.assertEqual(resolve_issue_target('https://github.com/a/b/issues/7', config, 'delltrak/lab'),
                         ('a/b', 7, 'explicit'))
        self.assertEqual(repo_note('delltrak/lab', 7, 'recent', 'pt'),
                         'Olhando delltrak/lab#7 (último repositório que investigamos). Se for outro, manda o link.')
        self.assertTrue(repo_note('demo/default', 7, 'default', 'en').startswith('Looking at demo/default#7'))
        self.assertIsNone(repo_note('a/b', 7, 'explicit', 'pt'))
        # On the line's own default repository the note is noise.
        self.assertIsNone(repo_note('demo/default', 7, 'default', 'pt', default='demo/default'))
        self.assertIsNone(repo_note('demo/default', 7, 'recent', 'pt', default='demo/default'))
        self.assertIsNotNone(repo_note('delltrak/lab', 7, 'recent', 'pt', default='demo/default'))

    def test_recent_repo_window(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            self.addCleanup(store.db.close)
            self.assertIsNone(store.recent_repo())
            run_id, _ = store.begin('delltrak/lab', 7, 'fp')
            store.finish(run_id, {'ok': True})
            self.assertEqual(store.recent_repo(), 'delltrak/lab')
            store.db.execute("UPDATE runs SET completed='2020-01-01T00:00:00+00:00'")
            self.assertIsNone(store.recent_repo())

    def test_recent_repo_follows_time_not_run_id(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            self.addCleanup(store.db.close)
            first, _ = store.begin('acme/app', 10, 'fp-a')
            store.finish(first, {'ok': True})
            store.db.execute("UPDATE runs SET completed='2026-09-23T10:00:00+00:00' WHERE id=?", (first,))
            second, _ = store.begin('delltrak/lab', 5, 'fp-b')
            store.finish(second, {'ok': True})
            store.db.execute("UPDATE runs SET completed='2026-09-23T10:05:00+00:00' WHERE id=?", (second,))
            self.assertEqual(store.recent_repo(hours=10**6), 'delltrak/lab')
            # A forced retry of the first issue updates its old row, keeping the old (lower) id.
            again, fresh = store.begin('acme/app', 10, 'fp-a', force=True)
            self.assertEqual((again, fresh), (first, True))
            store.finish(again, {'ok': True})
            self.assertEqual(store.recent_repo(), 'acme/app')
            # A cache hit on the lab issue only touches issues.checked.
            store.db.execute("INSERT OR IGNORE INTO issues(repo,number,title) VALUES('delltrak/lab',5,'#5')")
            store.checked('delltrak/lab', 5)
            self.assertEqual(store.recent_repo(), 'delltrak/lab')


class SkillsConfigTests(unittest.TestCase):
    def test_risky_bundled_skills_are_off_and_playbook_ships(self):
        text = (ROOT / 'runtime' / 'mcp-watson.yaml').read_text()
        disabled = text.split('plow_chat: &watson_off [', 1)[1].split(']', 1)[0]
        self.assertIn('plow_email: *watson_off', text)  # the line's email platform gets the same list
        names = {n.strip() for n in disabled.split(',')}
        for risky in ('codex', 'claude-code', 'opencode', 'github', 'computer-use'):
            self.assertIn(risky, names)
        for kept in ('hermes-agent', 'plow-invite', 'owners-mac', 'plow-connectors', 'google-workspace'):
            self.assertNotIn(kept, names)
        self.assertIn('external_dirs: [/opt/watson-triage/skills]', text)

    def test_playbook_frontmatter(self):
        skill = (ROOT / 'skills' / 'watson' / 'watson-playbook' / 'SKILL.md').read_text()
        front = skill.split('---', 2)[1]
        self.assertIn('name: watson-playbook', front)
        description = re.search(r'description: "([^"]+)"', front).group(1)
        self.assertLessEqual(len(description), 60)
        self.assertIn('session_platforms: [plow_chat]', front)
        for ref in ('investigation', 'connect', 'squad', 'closing'):
            self.assertTrue((ROOT / 'skills/watson/watson-playbook/references' / f'{ref}.md').exists())

    def test_persona_keeps_the_hard_rules(self):
        persona = (ROOT / 'runtime' / 'persona.md').read_text()
        for rule in ('RULE #1', 'speak_this', 'speak_first', 'review_note', 'repo_note', 'never merge',
                     'backticks', 'cannot** change the squad', 'watson-playbook', 'Never** save'):
            self.assertIn(rule, persona)


if __name__ == '__main__':
    unittest.main()
