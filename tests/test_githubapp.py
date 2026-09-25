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
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from watson import cli, githubapp
from watson.core import WatsonError
from watson.githubapp import App, connect, describe

ROOT = Path(__file__).resolve().parents[1]
DEVICE_URL = 'https://github.com/login/device/code'
USER_URL = 'https://api.github.com/user'
DEVICE = ('POST', DEVICE_URL, {'device_code': 'DEVICE-CODE-40', 'user_code': 'ABCD-1234',
                               'verification_uri': 'https://github.com/login/device', 'expires_in': 900, 'interval': 5})
PENDING = ('POST', githubapp.TOKEN_URL, {'error': 'authorization_pending'})
GRANTED = ('POST', githubapp.TOKEN_URL, {'access_token': 'ghu_A', 'refresh_token': 'ghr_A', 'expires_in': 28800,
                                         'refresh_token_expires_in': 15897600, 'token_type': 'bearer', 'scope': ''})
USER = ('GET', USER_URL, {'login': 'octocat'})
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

    def ask(self):
        path = self.requests / githubapp.REQUEST
        path.touch()
        os.utime(path, (self.t, self.t))

    def published(self):
        return json.loads(self.status.read_text())

    def token(self):
        return json.loads((self.store / 'token.json').read_text())

    def save(self, expires_in):
        self.app().save({'access_token': 'ghu_OLD', 'refresh_token': 'ghr_OLD', 'expires_in': expires_in})

    def test_connect_runs_the_device_flow_and_keeps_every_secret_root_only(self):
        self.ask()
        app = self.app(DEVICE, PENDING, ('POST', githubapp.TOKEN_URL, {'error': 'slow_down', 'interval': 10}),
                       GRANTED, USER)
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
                                             'install_url': 'https://github.com/apps/watson-triage/installations/new'})
        self.assertEqual(stat.S_IMODE(self.status.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.status.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.store / 'token.json').stat().st_mode), 0o600)
        self.assertEqual(self.token(), {'access_token': 'ghu_A', 'refresh_token': 'ghr_A',
                                        'expires_at': 1_000_000.0 + 20 + 28800})
        self.assertFalse((self.requests / githubapp.REQUEST).exists())
        poll = self.github.form(1)
        self.assertEqual(poll, {'client_id': ['Iv23client'], 'device_code': ['DEVICE-CODE-40'],
                                'grant_type': ['urn:ietf:params:oauth:grant-type:device_code']})
        self.assertEqual(self.github.form(0), {'client_id': ['Iv23client']})
        for request in self.github.calls[:-1]:  # no bearer on the OAuth endpoints; only /user gets one
            self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(self.github.calls[-1].get_header('Authorization'), 'Bearer ghu_A')

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
        self.assertFalse((self.requests / githubapp.REQUEST).exists())
        self.assertEqual((self.published()['state'], self.github.calls), ('disconnected', []))

    def test_only_a_regular_file_is_a_request(self):
        target = self.root / 'precious'
        target.write_text('keep')
        path = self.requests / githubapp.REQUEST
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

    def test_wait_answers_the_owner_without_starting_a_pass_early(self):
        self.ask()
        self.app(DEVICE, GRANTED, USER).wait(30)
        self.assertEqual(self.published()['state'], 'connected')
        self.assertFalse((self.requests / githubapp.REQUEST).exists())
        self.assertTrue(1_000_000.0 + 30 <= self.t < 1_000_000.0 + 32)  # returned at the tick, not on the answer

    def test_refreshes_between_passes_without_a_client_secret(self):
        self.save(3600)  # under the two-hour margin
        new = {'access_token': 'ghu_NEW', 'refresh_token': 'ghr_NEW', 'expires_in': 28800}
        self.assertEqual(self.app(('POST', githubapp.TOKEN_URL, new), USER).prepare(), 'ghu_NEW')
        self.assertEqual(self.github.form(0), {'client_id': ['Iv23client'], 'grant_type': ['refresh_token'],
                                               'refresh_token': ['ghr_OLD']})
        self.assertEqual(self.token(), {'access_token': 'ghu_NEW', 'refresh_token': 'ghr_NEW',
                                        'expires_at': self.t + 28800})
        self.assertEqual(self.published()['login'], 'octocat')

    def test_a_connected_pass_costs_no_github_call(self):
        self.save(5 * 3600)
        self.assertEqual(self.app(USER).prepare(), 'ghu_OLD')  # no status yet, as after a restart: checked once
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
        self.app().save({'access_token': 'ghu_FOREVER'})
        self.t += 10 ** 8
        self.assertEqual(self.app(USER).prepare(), 'ghu_FOREVER')
        self.assertEqual(self.token()['expires_at'], None)

    def test_a_connect_request_rechecks_and_a_revoked_token_asks_to_reconnect(self):
        self.save(5 * 3600)
        self.app(USER).prepare()
        self.assertEqual(self.published()['state'], 'connected')
        self.ask()  # connected, so only the owner's request makes this call
        self.assertIsNone(self.app(('GET', USER_URL, refused(401))).prepare())
        self.assertEqual((self.published()['state'], self.published()['detail']), ('reconnect', 'github_refused'))
        self.assertFalse((self.store / 'token.json').exists())

    def test_the_chat_asks_then_relays_roots_answer(self):
        def root_answers(_):
            self.t += 1
            self.app().publish('pending', user_code='ABCD-1234', verification_uri='https://github.com/login/device',
                               expires_at=self.t + 900)
        answer = connect(status=self.status, requests=self.requests, sleep=root_answers, clock=self.clock)
        self.assertEqual(answer, {'state': 'pending', 'user_code': 'ABCD-1234', 'minutes_left': 15,
                                  'verification_uri': 'https://github.com/login/device',
                                  'install_url': 'https://github.com/apps/watson-triage/installations/new'})
        request = self.requests / githubapp.REQUEST
        self.assertTrue(request.is_file())
        request.unlink()
        self.t += 120  # asking again while the code is good returns it, with no new request
        again = connect(status=self.status, requests=self.requests, sleep=lambda _: self.fail('asked again'),
                        clock=self.clock)
        self.assertEqual((again['user_code'], again['minutes_left'], request.exists()), ('ABCD-1234', 13, False))
        self.t += 900  # expired: a new request, unanswered while a pass runs
        busy = connect(status=self.status, requests=self.requests, wait_s=0, clock=self.clock)
        self.assertEqual((busy['queued'], request.exists()), (True, True))
        with self.assertRaises(WatsonError):
            connect(status=self.status, requests=self.root / 'missing', wait_s=0, clock=self.clock)
        self.status.unlink()  # root has never answered here: not a pass to wait for
        self.assertEqual(connect(status=self.status, requests=self.requests, wait_s=0, clock=self.clock),
                         {'state': 'unknown'})

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
        chat = partial(connect, status=self.status, requests=self.requests, sleep=root_answers, clock=self.clock)
        with mock.patch('watson.cli.connect', chat):
            self.assertEqual(run('github', 'connect')['user_code'], 'ABCD-1234')
        self.assertTrue((self.requests / githubapp.REQUEST).is_file())
        self.assertFalse(home.exists())  # no Store: nothing created, and no lock to wait on mid-pass
        run('init', '--repo', 'octo/repo', '--assignee', 'octocat')
        self.app().publish('connected', login='octocat')
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


if __name__ == '__main__':
    unittest.main()
