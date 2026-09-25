import fcntl
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from watson import cli, githubapp
from watson.core import WatsonError, private_json
from watson.githubapp import App, describe, request
from watson.workflow import NOTICE
from test_watson import CONFIG

ROOT = Path(__file__).resolve().parents[1]
DEVICE_URL = 'https://github.com/login/device/code'
USER_URL = 'https://api.github.com/user'
DEVICE = ('POST', DEVICE_URL, {'device_code': 'DEVICE-CODE-40', 'user_code': 'ABCD-1234',
                               'verification_uri': 'https://github.com/login/device', 'expires_in': 900, 'interval': 5})
PENDING = ('POST', githubapp.TOKEN_URL, {'error': 'authorization_pending'})
GRANTED = ('POST', githubapp.TOKEN_URL, {'access_token': 'ghu_A', 'refresh_token': 'ghr_A', 'expires_in': 28800,
                                         'refresh_token_expires_in': 15897600, 'token_type': 'bearer', 'scope': ''})
USER = ('GET', USER_URL, {'login': 'octocat'})
REVOKE_URL = 'https://api.github.com/credentials/revoke'
INSTALLATIONS_URL = 'https://api.github.com/user/installations?per_page=100'
LISTED = ('GET', INSTALLATIONS_URL, {'total_count': 0, 'installations': []})  # installed nowhere yet
# Everything that must stay in the root process: never in a log line, and the tokens never in the status.
SECRETS = ('DEVICE-CODE', 'ghu_', 'ghr_')


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeGitHub:
    """A urllib opener answering a script of (method, url, answer or exception) in order."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        method, url, answer = self.script.pop(0)
        assert (request.get_method(), request.full_url) == (method, url), (request.get_method(), request.full_url)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode())

    def form(self, index):
        return urllib.parse.parse_qs(self.calls[index].data.decode())


def refused(code):
    return urllib.error.HTTPError(USER_URL, code, 'refused', {}, io.BytesIO(b''))


class GitHubAppTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store, self.requests = self.root / 'store', self.root / 'home'
        self.status = self.root / 'run' / 'status.json'
        self.store.mkdir(mode=0o700)
        self.requests.mkdir()
        self.t, self.slept, self.logs = 1_000_000.0, [], []

    def tearDown(self):
        for line in self.logs:  # every flow below: the operator's log carries no token and no code
            for secret in SECRETS + ('ABCD-1234',):
                self.assertNotIn(secret, line)

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def app(self, *script):
        self.github = FakeGitHub(script)
        return App('Iv23client', 'watson-triage', store=self.store, status=self.status, requests=self.requests,
                   opener=self.github, sleep=self.sleep, clock=self.clock, log=self.logs.append)

    def ask(self, action='connect'):
        path = self.requests / githubapp.REQUEST.format(action)
        path.touch()
        os.utime(path, (self.t, self.t))

    def published(self):
        return json.loads(self.status.read_text())

    def token(self):
        return json.loads((self.store / 'token.json').read_text())

    def save(self, expires_in, connected_at=None):  # None: saved before connections were dated
        self.app().save({'access_token': 'ghu_OLD', 'refresh_token': 'ghr_OLD', 'expires_in': expires_in}, connected_at)

    def test_connect_runs_the_device_flow_and_keeps_every_secret_root_only(self):
        self.ask()
        app = self.app(DEVICE, PENDING, ('POST', githubapp.TOKEN_URL, {'error': 'slow_down', 'interval': 10}),
                       GRANTED, USER, LISTED)
        seen, publish = [], app.publish
        app.publish = lambda state, **fields: (publish(state, **fields), seen.append(self.status.read_text()))
        self.assertEqual(app.prepare(), 'ghu_A')
        self.assertEqual(self.slept, [5, 5, 10])  # slow_down widened the interval
        self.assertEqual(self.github.script, [])
        pending = json.loads(seen[0])
        self.assertEqual((pending['state'], pending['user_code'], pending['verification_uri'], pending['expires_at']),
                         ('pending', 'ABCD-1234', 'https://github.com/login/device', 1_000_000.0 + 900))
        for text in seen:  # the chat may read the user code; never the device code or a token
            for secret in SECRETS:
                self.assertNotIn(secret, text)
        self.assertEqual(self.published(), {'state': 'connected', 'login': 'octocat', 'updated_at': self.t,
                                             'connected_at': 1_000_000.0 + 20, 'repositories': [], 'repository_count': 0,
                                             'install_url': 'https://github.com/apps/watson-triage/installations/new'})
        self.assertEqual(stat.S_IMODE(self.status.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.status.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.store / 'token.json').stat().st_mode), 0o600)
        self.assertEqual(self.token(), {'access_token': 'ghu_A', 'refresh_token': 'ghr_A',
                                        'expires_at': 1_000_000.0 + 20 + 28800, 'connected_at': 1_000_000.0 + 20})
        self.assertFalse((self.requests / githubapp.REQUEST.format('connect')).exists())
        poll = self.github.form(1)
        self.assertEqual(poll, {'client_id': ['Iv23client'], 'device_code': ['DEVICE-CODE-40'],
                                'grant_type': ['urn:ietf:params:oauth:grant-type:device_code']})
        self.assertEqual(self.github.form(0), {'client_id': ['Iv23client']})
        for request in self.github.calls[:-2]:  # no bearer on the OAuth endpoints; only the API gets one
            self.assertIsNone(request.get_header('Authorization'))
        for request in self.github.calls[-2:]:
            self.assertEqual(request.get_header('Authorization'), 'Bearer ghu_A')

    def test_a_refused_or_lapsed_code_leaves_no_token_and_says_which(self):
        for error, state in (('access_denied', 'denied'), ('expired_token', 'expired'),
                             ('incorrect_device_code', 'failed')):
            with self.subTest(error=error):
                self.ask()
                self.assertIsNone(self.app(DEVICE, ('POST', githubapp.TOKEN_URL, {'error': error})).prepare())
                self.assertEqual((self.published()['state'], self.published()['detail']), (state, error))
                self.assertFalse((self.store / 'token.json').exists())
        with self.subTest(error='the deadline'):
            self.ask()
            short = ('POST', DEVICE_URL, {**DEVICE[2], 'expires_in': 10})
            self.assertIsNone(self.app(short, PENDING, PENDING).prepare())
            self.assertEqual(self.published()['state'], 'expired')
            self.assertEqual(self.github.script, [])

    def test_github_unreachable_during_connect_is_an_answer_not_a_crash(self):
        dropped = http.client.IncompleteRead(b'{"err')  # the connection closed mid-answer
        for script in ([('POST', DEVICE_URL, urllib.error.URLError('offline'))],
                       [DEVICE, ('POST', githubapp.TOKEN_URL, dropped)]):
            with self.subTest(error=type(script[-1][2]).__name__):
                self.ask()
                self.assertIsNone(self.app(*script).prepare())
                self.assertEqual((self.published()['state'], self.published()['detail']),
                                 ('failed', 'github_unreachable'))

    def test_no_request_and_no_token_is_disconnected_and_calls_nothing(self):
        self.assertIsNone(self.app().prepare())
        self.assertEqual(self.published()['state'], 'disconnected')
        self.assertEqual(self.github.calls, [])

    def test_a_stale_request_is_dropped_not_answered(self):
        self.ask()
        self.t += githubapp.REQUEST_TTL_S + 1
        self.assertIsNone(self.app().prepare())
        self.assertFalse((self.requests / githubapp.REQUEST.format('connect')).exists())
        self.assertEqual((self.published()['state'], self.github.calls), ('disconnected', []))

    def test_only_a_regular_file_is_a_request(self):
        target = self.root / 'precious'
        target.write_text('keep')
        path = self.requests / githubapp.REQUEST.format('connect')
        for plant, remove in ((lambda: path.symlink_to(target), path.unlink), (path.mkdir, path.rmdir)):
            with self.subTest(plant=remove.__name__):
                plant()
                app, answered = self.app(), []
                prepare, app.prepare = app.prepare, lambda: answered.append(1)
                self.slept = []
                app.wait(10)  # neither is a request: no answer, no spin, nothing followed
                self.assertEqual((answered, self.slept), ([], [2] * 5))
                self.assertIsNone(prepare())  # nor for the pass itself: not consumed, no device flow
                self.assertEqual((target.read_text(), self.github.calls), ('keep', []))
                self.assertTrue(os.path.lexists(path))
                remove()

    def test_one_holder_at_a_time(self):
        with open(self.store / 'lock', 'a') as held:  # another process mid-refresh, say
            fcntl.flock(held, fcntl.LOCK_EX)
            second = threading.Thread(target=self.app().prepare)
            second.start()
            second.join(0.3)
            self.assertEqual((second.is_alive(), self.status.exists()), (True, False))
        second.join(5)
        self.assertEqual(self.published()['state'], 'disconnected')

    def test_wait_answers_the_owner_and_starts_a_pass_early_only_for_a_new_connection(self):
        self.ask()
        self.app(DEVICE, ('POST', githubapp.TOKEN_URL, {'error': 'access_denied'})).wait(30)
        self.assertEqual(self.published()['state'], 'denied')
        self.assertFalse((self.requests / githubapp.REQUEST.format('connect')).exists())
        self.assertTrue(1_000_000.0 + 30 <= self.t < 1_000_000.0 + 32)  # returned at the tick, not on the answer
        self.ask()
        approved = self.t + 5  # one poll after the code was issued
        self.app(DEVICE, GRANTED, USER, LISTED).wait(600)
        self.assertEqual((self.published()['state'], self.t), ('connected', approved))  # not ten minutes later
        self.ask()  # connected: asking again checks again, and waits for the tick
        start = self.t
        self.app(USER, LISTED).wait(30)
        self.assertEqual(self.published()['connected_at'], approved)
        self.assertTrue(start + 30 <= self.t < start + 32)

    def test_refreshes_between_passes_without_a_client_secret(self):
        self.save(3600, connected_at=999_000.0)  # under the two-hour margin
        new = {'access_token': 'ghu_NEW', 'refresh_token': 'ghr_NEW', 'expires_in': 28800}
        self.assertEqual(self.app(('POST', githubapp.TOKEN_URL, new), USER, LISTED).prepare(), 'ghu_NEW')
        self.assertEqual(self.github.form(0), {'client_id': ['Iv23client'], 'grant_type': ['refresh_token'],
                                               'refresh_token': ['ghr_OLD']})
        self.assertEqual(self.token(), {'access_token': 'ghu_NEW', 'refresh_token': 'ghr_NEW',
                                        'expires_at': self.t + 28800, 'connected_at': 999_000.0})
        # The same connection: a refresh is never announced as a new one.
        self.assertEqual((self.published()['login'], self.published()['connected_at']), ('octocat', 999_000.0))

    def test_a_connected_pass_costs_no_github_call(self):
        self.save(5 * 3600)
        self.assertEqual(self.app(USER, LISTED).prepare(), 'ghu_OLD')  # no status yet, as after a restart: checked once
        self.assertEqual(self.published()['state'], 'connected')
        self.assertEqual(self.app().prepare(), 'ghu_OLD')      # then nothing, and no refresh
        self.assertEqual(self.github.calls, [])

    def test_a_refused_refresh_forgets_the_token_and_asks_to_reconnect(self):
        self.save(3600)
        self.assertIsNone(self.app(('POST', githubapp.TOKEN_URL, {'error': 'bad_refresh_token'})).prepare())
        self.assertEqual((self.published()['state'], self.published()['detail']), ('reconnect', 'bad_refresh_token'))
        self.assertFalse((self.store / 'token.json').exists())

    def test_github_unreachable_keeps_the_token_and_hands_it_out_while_it_lasts(self):
        for left, handed, error in ((3600, 'ghu_OLD', urllib.error.URLError('down')), (3600, 'ghu_OLD', refused(502)),
                                    (3600, 'ghu_OLD', http.client.IncompleteRead(b'')),
                                    (30, None, urllib.error.URLError('down'))):
            with self.subTest(left=left, error=type(error).__name__):
                self.save(left)
                offline = ('POST', githubapp.TOKEN_URL, error)
                self.assertEqual(self.app(offline).prepare(), handed)
                self.assertEqual(self.token()['refresh_token'], 'ghr_OLD')  # kept for the next tick

    def test_a_token_that_never_expires_is_never_refreshed(self):
        self.app().save({'access_token': 'ghu_FOREVER'}, None)
        self.t += 10 ** 8
        self.assertEqual(self.app(USER, LISTED).prepare(), 'ghu_FOREVER')
        self.assertEqual(self.token()['expires_at'], None)

    def test_a_connect_request_rechecks_and_a_revoked_token_asks_to_reconnect(self):
        self.save(5 * 3600)
        self.app(USER, LISTED).prepare()
        self.assertEqual(self.published()['state'], 'connected')
        self.ask()  # connected, so only the owner's request makes this call
        self.assertIsNone(self.app(('GET', USER_URL, refused(401))).prepare())
        self.assertEqual((self.published()['state'], self.published()['detail']), ('reconnect', 'github_refused'))
        self.assertFalse((self.store / 'token.json').exists())

    def test_the_owner_picks_from_the_repositories_the_app_can_read(self):
        def repo(name, pushed):
            return {'full_name': name, 'private': name == 'octo/secret', 'pushed_at': pushed,
                    'owner': {'login': 'octo'}, 'permissions': {'admin': True}, 'clone_url': f'https://github.com/{name}.git'}

        def page(installation, *repos, count=None):
            return ('GET', f'https://api.github.com/user/installations/{installation}/repositories?per_page=100',
                    {'total_count': count or len(repos), 'repository_selection': 'selected', 'repositories': list(repos)})
        # Eleven installations: ten are read, a page each, and ten of their eleven repositories published.
        installed = ('GET', INSTALLATIONS_URL, {'total_count': 11, 'installations': [{'id': n} for n in range(1, 12)]})
        pages = [page(1, repo('octo/empty', None), repo('octo/secret', '2026-09-20T10:00:00Z'), count=150)]
        pages += [page(n, repo(f'octo/r{n}', f'2026-09-{n:02d}T10:00:00Z')) for n in range(2, 11)]
        self.save(5 * 3600)
        self.assertEqual(self.app(USER, installed, *pages).prepare(), 'ghu_OLD')
        self.assertEqual(self.github.script, [])
        published = self.published()
        self.assertEqual((published['state'], published['repository_count']), ('connected', 159))
        self.assertEqual([r['full_name'] for r in published['repositories']],  # latest push first; never pushed, cut
                         ['octo/secret'] + [f'octo/r{n}' for n in range(10, 1, -1)])
        self.assertEqual(published['repositories'][0],  # names and dates, nothing else GitHub sent
                         {'full_name': 'octo/secret', 'private': True, 'pushed_at': '2026-09-20T10:00:00Z'})
        for request in self.github.calls:
            self.assertEqual(request.get_header('Authorization'), 'Bearer ghu_OLD')
        self.ask()  # "I installed it": asking again lists again
        self.app(USER, ('GET', INSTALLATIONS_URL, {'total_count': 1, 'installations': [{'id': 12}]}),
                 page(12, repo('octo/new', '2026-09-21T10:00:00Z'))).prepare()
        self.assertEqual((self.published()['repositories'][0]['full_name'], self.published()['repository_count']),
                         ('octo/new', 1))

    def test_a_list_github_will_not_give_leaves_the_connection_standing(self):
        self.save(5 * 3600)
        self.assertEqual(self.app(USER, ('GET', INSTALLATIONS_URL, refused(502))).prepare(), 'ghu_OLD')
        self.assertEqual(self.published()['state'], 'connected')
        self.assertNotIn('repositories', self.published())
        # Not a list of nothing: the next tick asks again, and once it has the list a pass costs no call.
        self.assertEqual(self.app(USER, LISTED).prepare(), 'ghu_OLD')
        self.assertEqual((self.published()['repositories'], self.published()['repository_count']), ([], 0))
        self.assertEqual(self.app().prepare(), 'ghu_OLD')
        self.assertEqual(self.github.calls, [])

    def test_the_owner_disconnects_and_it_stands_until_they_connect_again(self):
        self.save(5 * 3600, connected_at=999_000.0)
        self.ask('disconnect')
        self.app(('POST', REVOKE_URL, {})).wait(30)
        self.assertTrue(1_000_000.0 + 30 <= self.t < 1_000_000.0 + 32)  # answered at once, and no pass early
        revoke = self.github.calls[0]
        self.assertIsNone(revoke.get_header('Authorization'))  # GitHub refuses this call authenticated
        self.assertEqual(json.loads(revoke.data), {'credentials': ['ghu_OLD', 'ghr_OLD']})
        self.assertFalse((self.store / 'token.json').exists())
        self.assertFalse((self.requests / githubapp.REQUEST.format('disconnect')).exists())
        self.assertEqual((self.published()['state'], self.published()['detail'], self.published()['revoked']),
                         ('disconnected', 'by_owner', True))
        self.status.unlink()  # a restart empties /run; the owner's choice outlives it
        self.assertIsNone(self.app().prepare())
        self.assertEqual((self.published()['detail'], self.published()['revoked'], self.github.calls), ('by_owner', True, []))
        self.ask()  # a reconnect they abandon or deny leaves the choice standing, and the agent can tell
        self.app(DEVICE, ('POST', githubapp.TOKEN_URL, {'error': 'access_denied'})).prepare()
        self.assertEqual((self.published()['state'], self.published()['by_owner']), ('denied', True))
        self.ask()
        self.app(DEVICE, GRANTED, USER, LISTED).prepare()  # connecting again ends it
        self.assertNotIn('by_owner', self.published())
        (self.store / 'token.json').unlink()  # so a token lost after that is not the owner's doing
        self.app().prepare()
        self.assertEqual(self.published()['state'], 'disconnected')
        self.assertNotIn('detail', self.published())
        self.assertNotIn('by_owner', self.published())

    def test_a_disconnect_stands_however_github_answers_or_however_late_it_is_seen(self):
        # The owner is told whether GitHub took the revocation: nothing is left to retry it with.
        for name, error, revoked in (('422', refused(422), False), ('offline', urllib.error.URLError('offline'), False),
                                     ('late', None, True)):
            with self.subTest(name):
                self.save(5 * 3600)
                self.ask('disconnect')
                if error is None:
                    self.t += githubapp.REQUEST_TTL_S + 1  # after a long pass: still the owner's choice
                self.assertIsNone(self.app(('POST', REVOKE_URL, error or {})).prepare())
                self.assertFalse((self.store / 'token.json').exists())
                self.assertEqual((self.published()['detail'], self.published()['revoked']), ('by_owner', revoked))
        self.save(5 * 3600)
        self.ask('disconnect')
        self.assertIsNone(self.app(('POST', REVOKE_URL, urllib.error.URLError('offline'))).prepare())
        self.ask('disconnect')  # nothing left to revoke, and the refusal before is not forgotten
        self.assertIsNone(self.app().prepare())
        self.assertEqual((self.published()['detail'], self.published()['revoked'], self.github.calls), ('by_owner', False, []))

    def test_the_chat_asks_to_disconnect_even_with_a_code_pending(self):
        self.app().publish('pending', user_code='ABCD-1234', verification_uri='https://github.com/login/device',
                           expires_at=self.t + 900)

        def root_answers(_):
            self.t += 1
            self.app().publish('disconnected', detail='by_owner')
        self.assertEqual(request('disconnect', status=self.status, requests=self.requests, sleep=root_answers,
                                 clock=self.clock),
                         {'state': 'disconnected', 'detail': 'by_owner',
                          'install_url': 'https://github.com/apps/watson-triage/installations/new'})
        self.assertTrue((self.requests / githubapp.REQUEST.format('disconnect')).is_file())

    def test_the_chat_asks_then_relays_roots_answer(self):
        def root_answers(_):
            self.t += 1
            self.app().publish('pending', user_code='ABCD-1234', verification_uri='https://github.com/login/device',
                               expires_at=self.t + 900)
        chat = partial(request, 'connect', status=self.status, clock=self.clock)
        self.assertEqual(chat(requests=self.requests, sleep=root_answers),
                         {'state': 'pending', 'user_code': 'ABCD-1234', 'minutes_left': 15,
                          'verification_uri': 'https://github.com/login/device',
                          'install_url': 'https://github.com/apps/watson-triage/installations/new'})
        asked = self.requests / githubapp.REQUEST.format('connect')
        self.assertTrue(asked.is_file())
        asked.unlink()
        self.t += 120  # asking again while the code is good returns it, with no new request
        again = chat(requests=self.requests, sleep=lambda _: self.fail('asked again'))
        self.assertEqual((again['user_code'], again['minutes_left'], asked.exists()), ('ABCD-1234', 13, False))
        self.t += 900  # expired: a new request, unanswered while a pass runs
        busy = chat(requests=self.requests, wait_s=0)
        self.assertEqual((busy['queued'], asked.exists()), (True, True))
        with self.assertRaises(WatsonError):
            chat(requests=self.root / 'missing', wait_s=0)
        self.status.unlink()  # root has never answered here: not a pass to wait for
        self.assertEqual(chat(requests=self.requests, wait_s=0), {'state': 'unknown'})

    def test_the_chat_connects_before_init_without_the_store_and_status_shows_github(self):
        home = self.root / 'watson'

        def run(*argv):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['--home', str(home), *argv]), 0)
            return json.loads(out.getvalue())

        def root_answers(_):
            self.t += 1
            self.app().publish('pending', user_code='ABCD-1234', verification_uri='https://github.com/login/device',
                               expires_at=self.t + 900)
        chat = partial(request, status=self.status, requests=self.requests, sleep=root_answers, clock=self.clock)
        with mock.patch('watson.cli.request', chat):
            self.assertEqual(run('github', 'connect', '--language', 'pt')['user_code'], 'ABCD-1234')
        self.assertTrue((self.requests / githubapp.REQUEST.format('connect')).is_file())
        # No Store, so no lock to wait on mid-pass: only the language to announce the connection in.
        self.assertEqual([p.name for p in home.iterdir()], ['connect.json'])
        self.assertEqual(json.loads((home / 'connect.json').read_text()), {'language': 'pt'})
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        run('init', '--repo', 'octo/repo', '--assignee', 'octocat')
        self.app().publish('connected', login='octocat', connected_at=self.t)
        with mock.patch('watson.cli.read_status', partial(githubapp.read_status, self.status)):
            self.assertEqual(run('status')['github'], {
                'state': 'connected', 'login': 'octocat',
                'install_url': 'https://github.com/apps/watson-triage/installations/new'})

    def test_describe_leaves_out_the_timestamps(self):
        self.assertEqual(describe({}), {'state': 'unknown'})
        self.app().publish('connected', login='octocat')
        self.assertEqual(describe(self.published()),
                         {'state': 'connected', 'login': 'octocat',
                          'install_url': 'https://github.com/apps/watson-triage/installations/new'})

    def test_a_build_without_the_app_refuses_loudly(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('WATSON_GITHUB_')}
        run = subprocess.run([sys.executable, '-B', '-m', 'watson.githubapp', 'prepare'], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout), (1, ''))
        self.assertIn('WATSON_GITHUB_CLIENT_ID', run.stderr)
        self.assertIn('WATSON_GITHUB_APP_SLUG', run.stderr)


class AnnounceTests(unittest.TestCase):
    """The agent's half of a new connection: the owner is told at once, once."""
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home, self.status, self.sent, self.chats = self.root / 'watson', self.root / 'run' / 'status.json', [], 0
        test = self

        class Owner:
            def owner_chat(self):
                test.chats += 1
                return 'chat'

            def send(self, chat, body, media=None):
                test.sent.append(body)
                return {'message_uid': f'm{len(test.sent)}', 'chat_uid': chat}
        for target, value in (('watson.workflow.Plow', mock.Mock(**{'from_config.return_value': Owner()})),
                              ('watson.workflow.read_status', partial(githubapp.read_status, self.status))):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self, state, **fields):
        App('Iv23client', 'watson-triage', store=self.root, status=self.status).publish(state, **fields)

    def connected(self, at, *names):
        self.publish('connected', login='octocat', connected_at=at, repository_count=len(names), repositories=[
            {'full_name': name, 'private': False, 'pushed_at': '2026-09-20T10:00:00Z'} for name in names])

    def announce(self, code=0):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--home', str(self.home), 'github', 'announce']), code)

    def test_the_owner_hears_of_a_new_connection_once_in_the_language_they_connected_in(self):
        with redirect_stdout(io.StringIO()), mock.patch('watson.cli.request', return_value={}):
            cli.main(['--home', str(self.home), 'github', 'connect', '--language', 'pt'])
        self.connected(100.0, 'octo/a', 'octo/b')
        self.announce()
        self.announce()  # every tick asks; one connection is told once, with no Plow call to find that out
        self.assertEqual(self.sent, [NOTICE['pt']['connected_list'].format(
            login='octocat', repos='\n\n**1. octo/a**\n\n**2. octo/b**')])
        self.assertEqual(self.chats, 1)
        self.connected(200.0, 'octo/a')  # disconnected and connected again: a new connection
        self.announce()
        self.assertEqual(self.sent[1:], [NOTICE['pt']['connected_one'].format(login='octocat', repo='octo/a')])

    def test_with_nothing_installed_the_owner_gets_the_install_link_in_english_by_default(self):
        self.connected(100.0)
        self.announce()
        self.assertEqual(self.sent, [NOTICE['en']['connected_none'].format(
            login='octocat', url='https://github.com/apps/watson-triage/installations/new')])

    def test_a_configured_install_hears_it_is_reconnected_unless_it_is_quiet(self):
        reconnected = NOTICE['en']['reconnected'].format(login='octocat')
        for at, notify, told in ((100.0, False, []), (200.0, True, [reconnected])):
            with self.subTest(notify_owner=notify):
                private_json(self.home / 'config.json', {**CONFIG, 'notify_owner': notify})
                self.connected(at, 'octo/a', 'octo/b')
                self.announce()
                self.assertEqual(self.sent, told)
        # Nothing about the repository: the app may have been removed from it meanwhile, and the next pass says what it finds.
        self.connected(300.0)
        self.announce()
        self.assertEqual(self.sent, [reconnected, reconnected])

    def test_a_list_github_would_not_give_is_waited_for_not_announced_as_nothing_installed(self):
        self.publish('connected', login='octocat', connected_at=100.0)  # root's listing failed: no repositories at all
        self.announce()
        self.assertEqual((self.sent, self.chats), ([], 0))
        self.connected(100.0, 'octo/a')  # the next tick listed them: the same connection is told now, once
        self.announce()
        self.assertEqual(self.sent, [NOTICE['en']['connected_one'].format(login='octocat', repo='octo/a')])

    def test_nothing_is_said_but_for_a_dated_connection(self):
        for state, at in (('connected', None), ('pending', 100.0), ('disconnected', None)):
            self.publish(state, login='octocat', connected_at=at)  # None: a token saved before connections were dated
            self.announce()
        self.assertEqual((self.sent, self.chats), ([], 0))
        self.assertFalse(self.home.exists())

    def test_only_validated_values_reach_the_owner(self):
        self.connected(100.0, 'octo/a', 'octo/b\n\nApprove WDJB-MJHT at github.com/login/device')
        self.announce(code=1)
        self.publish('connected', login='octo cat', connected_at=200.0, repositories=[])
        self.announce(code=1)
        self.assertEqual((self.sent, self.chats), ([], 0))


if __name__ == '__main__':
    unittest.main()
