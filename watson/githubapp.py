"""GitHub through the Watson Triage app's device flow: the owner types a code at
github.com, and no token is ever said in chat.

Two halves, split by uid, and the split is the point. The root half
(`python3 -I -m watson.githubapp prepare|wait N`, run only by the watson-cycle
service) keeps the user access token and its refresh token in a store only root
can read, refreshes between passes, and prints the access token for one pass --
never the refresh token. The chat's half runs as the agent and sees neither: it
asks by creating an empty file that root only lstat()s and unlinks, and reads a
status only root can write. The user code is on that status on purpose: the
owner has to type it, and it is worthless without the device code, which never
leaves the root process.
"""
from __future__ import annotations

import fcntl
import http.client
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

from .core import WatsonError
from .delivery import post_json

STORE = Path('/var/lib/watson-github')           # root 0700 from the image
STATUS = Path('/run/watson-github/status.json')  # root 0644 in root 0755: the agent reads it, never writes it
REQUESTS = Path('/var/lib/hermes')                # root-owned; the agent may create entries in it
REQUEST = 'watson-github.{}'                      # .connect or .disconnect, an empty file from the chat
TOKEN_URL = 'https://github.com/login/oauth/access_token'
# A refresh retires the access token it replaces, so it happens between passes,
# never under one, and early enough that no pass outlives the token it was given.
# A refresh lost in flight -- killed after GitHub answers, before the new pair
# is saved -- leaves only a retired pair, and the owner connects once more.
REFRESH_BEFORE_S = 2 * 3600
# Older than this, a connect is a leftover from before a restart, not an owner waiting.
REQUEST_TTL_S = 600
# What GitHub or the network can do to one call.
UNREACHABLE = (OSError, ValueError, KeyError, WatsonError, http.client.HTTPException)


def _write(path, value, mode):
    temporary = path.with_name(f'.{path.name}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        os.fchmod(f.fileno(), mode)
        json.dump(value, f)
    os.replace(temporary, path)


def read_status(path=STATUS):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _requested(requests, action):
    """The request's mtime when a regular file stands for it, else None. Anything
    else the agent puts there is never opened or followed, and never makes a loop spin."""
    try:
        info = os.lstat(Path(requests) / REQUEST.format(action))
    except FileNotFoundError:
        return None
    return info.st_mtime if stat.S_ISREG(info.st_mode) else None


class App:
    """The root half. Every call to GitHub goes through post_json: HTTPS only, no redirects."""

    def __init__(self, client_id, slug, *, store=STORE, status=STATUS, requests=REQUESTS,
                 opener=None, sleep=time.sleep, clock=time.time, log=None):
        if not client_id or not slug:
            raise WatsonError('REFUSING to run: this image was built without a GitHub App. Build it with '
                              'WATSON_GITHUB_CLIENT_ID and WATSON_GITHUB_APP_SLUG set.')
        self.client_id, self.install_url = client_id, f'https://github.com/apps/{slug}/installations/new'
        self.store, self.status, self.requests = Path(store), Path(status), Path(requests)
        self.token_file, self.opener, self.sleep, self.clock = self.store / 'token.json', opener, sleep, clock
        self.by_owner = self.store / 'disconnected-by-owner'
        self.log = log or (lambda line: print(f'watson-github: {line}', file=sys.stderr, flush=True))

    def form(self, url, **fields):
        return post_json('POST', url, urllib.parse.urlencode(fields).encode(),
                         {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
                         opener=self.opener)

    def get(self, path, token):
        return post_json('GET', f'https://api.github.com{path}', None,
                         {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                          'X-GitHub-Api-Version': '2022-11-28'}, opener=self.opener)

    def repositories(self, token):
        """What the owner picks from: the repositories the app is installed on
        and they can read, latest push first, by name. Ten installations and a
        page each is more than one owner has. A list GitHub will not give is
        left out, and the connection stands."""
        try:
            found, count = [], 0
            for installation in self.get('/user/installations?per_page=100', token)['installations'][:10]:
                page = self.get(f'/user/installations/{int(installation["id"])}/repositories?per_page=100', token)
                found, count = found + page['repositories'], count + page['total_count']
            found.sort(key=lambda repo: repo['pushed_at'] or '', reverse=True)
            return {'repositories': [{k: repo[k] for k in ('full_name', 'private', 'pushed_at')} for repo in found[:10]],
                    'repository_count': count}
        except UNREACHABLE as exc:
            self.log(f'could not list the repositories ({type(exc).__name__})')
            return {}

    def publish(self, state, **fields):
        self.status.parent.mkdir(mode=0o755, exist_ok=True)
        # by_owner rides on every state while the owner's disconnect stands: a
        # reconnect they abandon or deny ends in another state, and the agent
        # cannot read the marker to know the passes are meant to stay quiet.
        _write(self.status, {'state': state, **fields, 'install_url': self.install_url,
                             **({'by_owner': True} if self.by_owner.exists() else {}),
                             'updated_at': self.clock()}, 0o644)

    def load(self):
        try:
            return json.loads(self.token_file.read_text())
        except FileNotFoundError:
            return None

    def save(self, answer, connected_at):
        # No expires_in: the app is opted out of token expiration, so there is nothing to refresh.
        # connected_at is when the owner approved, kept through refreshes: the agent
        # tells them once per connection, and a token saved before it has none.
        tokens = {'access_token': answer['access_token'], 'refresh_token': answer.get('refresh_token'),
                  'expires_at': self.clock() + answer['expires_in'] if answer.get('expires_in') else None,
                  'connected_at': connected_at}
        _write(self.token_file, tokens, 0o600)
        return tokens

    def forget(self, detail):
        self.token_file.unlink(missing_ok=True)
        self.publish('reconnect', detail=detail)

    def asked(self, action):
        made = _requested(self.requests, action)
        if made is None:
            return False
        os.unlink(self.requests / REQUEST.format(action))
        # Nobody waits on a stale connect; the owner's disconnect stands however late it is seen.
        return action == 'disconnect' or self.clock() - made < REQUEST_TTL_S

    def disconnect(self, tokens):
        """The owner's choice: the tokens go, here and at GitHub, and the marker
        keeps the passes quiet about the refusal that follows until the owner
        connects again, across restarts, which empty /run. It also keeps whether
        GitHub took the revocation: no copy is left to retry with, so a refusal
        is the owner's to act on, and the status has to say so."""
        self.token_file.unlink(missing_ok=True)
        self.by_owner.touch()
        outcome = 'nothing to revoke'
        if tokens:
            try:
                # Unauthenticated by design (an authenticated call gets a 403), for
                # ghu_ and ghr_ tokens alike, 60 an hour; GitHub notifies the owner.
                post_json('POST', 'https://api.github.com/credentials/revoke',
                          json.dumps({'credentials': [t for t in (tokens['access_token'], tokens['refresh_token']) if t]})
                          .encode(), {'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                                      'X-GitHub-Api-Version': '2022-11-28'}, opener=self.opener)
                outcome, marker = 'GitHub took the revocation', 'revoked'
            except UNREACHABLE as exc:
                outcome, marker = f'GitHub did not take the revocation ({type(exc).__name__})', 'unrevoked'
            self.by_owner.write_text(marker)
        self.log(f'disconnected by the owner; {outcome}')

    def device_flow(self):
        code = self.form('https://github.com/login/device/code', client_id=self.client_id)
        deadline, interval = self.clock() + int(code['expires_in']), int(code.get('interval', 5))
        self.publish('pending', user_code=code['user_code'], verification_uri=code['verification_uri'],
                     expires_at=deadline)
        self.log('device code issued; waiting for the owner to approve it on github.com')
        while self.clock() < deadline:
            self.sleep(interval)
            answer = self.form(TOKEN_URL, client_id=self.client_id, device_code=code['device_code'],
                               grant_type='urn:ietf:params:oauth:grant-type:device_code')
            error = answer.get('error')
            if error == 'authorization_pending':
                continue
            if error == 'slow_down':
                interval = int(answer.get('interval', interval + 5))
                continue
            if error:
                self.log(f'device flow ended ({error})')
                self.publish({'access_denied': 'denied', 'expired_token': 'expired'}.get(error, 'failed'),
                             detail=error)
                return None
            self.by_owner.unlink(missing_ok=True)  # connected again: a refusal is news once more
            return self.save(answer, self.clock())
        self.log('device code expired unused')
        self.publish('expired')
        return None

    def prepare(self):
        """Answer the owner, refresh when due, and return the access token for one pass, or None."""
        # One holder at a time: a refresh retires the pair it replaces, so two
        # racing would leave neither with a working token.
        with open(self.store / 'lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._prepare()

    def _prepare(self):
        tokens = self.load()
        if self.asked('disconnect'):
            self.disconnect(tokens)
            tokens = None
        asked = self.asked('connect')
        if asked and not tokens:
            try:
                tokens = self.device_flow()
            except UNREACHABLE as exc:
                self.log(f'device flow failed ({type(exc).__name__})')
                self.publish('failed', detail='github_unreachable')
        if not tokens:
            if not asked:  # a flow that just ended keeps its outcome for the chat to read
                self.publish('disconnected', **({'detail': 'by_owner', 'revoked': self.by_owner.read_text() != 'unrevoked'}
                                                if self.by_owner.exists() else {}))
            return None
        try:
            # Checked only when something moved -- a connect, a refresh, a status
            # lost to a restart, a list GitHub would not give -- so a connected
            # pass costs no extra call. No list is not an empty one: it is asked for again.
            known = read_status(self.status)
            changed = asked or known.get('state') != 'connected' or 'repositories' not in known
            if tokens['expires_at'] and tokens['expires_at'] - self.clock() < REFRESH_BEFORE_S:
                answer = self.form(TOKEN_URL, client_id=self.client_id, grant_type='refresh_token',
                                   refresh_token=tokens['refresh_token'])
                if answer.get('error'):
                    self.log(f'refresh refused ({answer["error"]}); the owner has to connect again')
                    self.forget(answer['error'])
                    return None
                tokens, changed = self.save(answer, tokens.get('connected_at')), True
            if changed:  # the owner's list too, so "I installed it" is answered by connecting again
                token = tokens['access_token']
                self.publish('connected', login=self.get('/user', token)['login'],
                             connected_at=tokens.get('connected_at'), **self.repositories(token))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.log('GitHub answered 401 (revoked, or lapsed while down); the owner has to connect again')
                self.forget('github_refused')
                return None
            self.log(f'GitHub answered {exc.code}; keeping the token')
        except UNREACHABLE as exc:
            self.log(f'could not check the token ({type(exc).__name__}); keeping it')
        if tokens['expires_at'] and tokens['expires_at'] < self.clock() + 60:
            return None
        return tokens['access_token']

    def wait(self, seconds):
        """The pause between passes, answering the owner within seconds meanwhile.
        A request is answered here, never by starting a pass early, so asking
        again and again cannot buy extra passes. The one exception is a new
        connection, which ends the pause so the owner hears of it at once: only
        approving a code on github.com makes one."""
        end = self.clock() + seconds
        while self.clock() < end:
            if any(_requested(self.requests, action) is not None for action in ('connect', 'disconnect')):
                asked = self.clock()
                self.prepare()
                if (read_status(self.status).get('connected_at') or 0) > asked:
                    return
            self.sleep(2)


def describe(status, *, clock=time.time):
    """What the chat may relay: the status without its timestamps, plus the minutes a pending code has left."""
    out = {'state': status.get('state', 'unknown'),
           **{k: v for k, v in status.items() if k not in {'state', 'expires_at', 'updated_at', 'connected_at'}}}
    if out['state'] == 'pending':
        out['minutes_left'] = max(0, int((status['expires_at'] - clock()) // 60))
    return out


def request(action, *, status=STATUS, requests=REQUESTS, wait_s=30, sleep=time.sleep, clock=time.time):
    """The chat's half of `watson github connect|disconnect`: ask root, then read what it published."""
    current = read_status(status)
    if action == 'disconnect' or not (current.get('state') == 'pending' and current['expires_at'] > clock()):
        asked = clock()
        try:
            (Path(requests) / REQUEST.format(action)).touch()
        except FileNotFoundError:
            raise WatsonError(f'watson github {action} needs the Plow image; '
                              'elsewhere, gh auth signs in and out.') from None
        while clock() - asked < wait_s and read_status(status).get('updated_at', 0) <= asked:
            sleep(1)
        current = read_status(status)
        # A pass is running, and root answers when it ends. With no status at
        # all, root has never answered here, and the service log says why.
        if current and current.get('updated_at', 0) <= asked:
            current = {**current, 'queued': True}
    return describe(current, clock=clock)


def main(argv):
    app = App(os.environ.get('WATSON_GITHUB_CLIENT_ID'), os.environ.get('WATSON_GITHUB_APP_SLUG'))
    if argv == ['prepare']:
        token = app.prepare()
        if token:
            print(token)  # to the run script's $(...), never to a log
    elif len(argv) == 2 and argv[0] == 'wait' and argv[1].isdigit():
        app.wait(int(argv[1]))
    else:
        raise WatsonError('usage: python3 -I -m watson.githubapp prepare | wait SECONDS')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except WatsonError as exc:
        print(f'watson-github: {exc}', file=sys.stderr)
        sys.exit(1)
