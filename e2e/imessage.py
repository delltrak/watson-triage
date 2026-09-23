"""Real iMessage transport for Watson E2E (macOS only).

Sends from the owner's Messages app to the Watson line and reads what Watson
actually sent: agent turns from Hermes' state.db, slash-command replies from the
gateway's "Sending response (N chars)" log line. Read-only on the container.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

CHAT = os.environ.get('WATSON_E2E_CHAT', 'any;-;+16503156604')
CONTAINER = os.environ.get('WATSON_E2E_CONTAINER', 'watson-triage-agent-1')
PY = '/opt/hermes/.venv/bin/python'

_PEEK = r'''
import json, sqlite3, sys
after = int(sys.argv[1])
c = sqlite3.connect('file:/var/lib/hermes/state.db?mode=ro', uri=True)
rows = c.execute("select id, role, coalesce(content,''), coalesce(tool_calls,'') from messages "
                 "where id > ? order by id", (after,)).fetchall()
last = c.execute("select coalesce(max(id),0) from messages").fetchone()[0]
print(json.dumps({'last': last, 'rows': rows}))
'''


def _exec(*args, user='10000'):
    return subprocess.run(['docker', 'exec', '-u', user, CONTAINER, *args],
                          capture_output=True, text=True, check=True).stdout


def peek(after):
    return json.loads(_exec(PY, '-c', _PEEK, str(after)))


def send(text):
    # Text goes in as an argument (AppleScript has no \u escapes; accents must pass through as is).
    subprocess.run(['osascript', '-e', 'on run argv',
                    '-e', f'tell application "Messages" to send (item 1 of argv) to chat id "{CHAT}"',
                    '-e', 'end run', '--', text], check=True, capture_output=True, text=True)


def expected_greeting(language):
    """What the owner's next plain greeting should get, computed BEFORE sending it
    (the greeting itself updates the onboarding state)."""
    code = ('import os, sys\n'
            'env = dict(x.split(b"=", 1) for x in open("/proc/%s/environ" % sys.argv[1], "rb").read()'
            '.split(b"\\0") if b"=" in x)\n'
            'os.environ["GH_TOKEN"] = env[b"GH_TOKEN"].decode()\n'
            'os.environ["HOME"] = "/var/lib/hermes"\n'
            'from watson import onboarding\n'
            'from watson.capabilities import capabilities_report, short_greeting\n'
            'from watson.roster import load\n'
            'home = "/var/lib/hermes/watson"\n'
            'report = capabilities_report(language=sys.argv[2], squad=load(home))\n'
            'state = onboarding.load(home)\n'
            'short = onboarding.decide(state, report, state.get("owner_uid")) == "short"\n'
            'sys.stdout.write(short_greeting(report, sys.argv[2]) if short else report["speak_this"])\n')
    pid = _exec('pgrep', '-o', '-f', 'hermes gateway run').strip()
    return _exec(PY, '-c', code, pid, language)


def reset_onboarding():
    _exec('rm', '-f', '/var/lib/hermes/watson/owner.json')


def speak_this(language):
    """The greeting copy Watson would send now (computed in the gateway's env)."""
    code = ('import os, sys\n'
            'env = dict(x.split(b"=", 1) for x in open("/proc/%s/environ" % sys.argv[1], "rb").read()'
            '.split(b"\\0") if b"=" in x)\n'
            'os.environ["GH_TOKEN"] = env[b"GH_TOKEN"].decode()\n'
            'os.environ["HOME"] = "/var/lib/hermes"\n'
            'from watson.capabilities import capabilities_report\n'
            'from watson.roster import load\n'
            'squad = load("/var/lib/hermes/watson")\n'
            'sys.stdout.write(capabilities_report(language=sys.argv[2], squad=squad)["speak_this"])\n')
    pid = _exec('pgrep', '-o', '-f', 'hermes gateway run').strip()
    return _exec(PY, '-c', code, pid, language)


def turn(text, timeout=600):
    """Send one message and wait for Watson's final reply to it (agent turn)."""
    base = peek(0)['last']
    started = time.time()
    send(text)
    user_id = None
    while time.time() - started < timeout:
        time.sleep(3)
        data = peek(base)
        for mid, role, content, _calls in data['rows']:
            if role == 'user' and user_id is None and content.strip().endswith(text.strip()):
                user_id = mid
        if user_id is None:
            continue
        time.sleep(1)
        rows = _turn_rows(peek(user_id)['rows'])
        finals = [c for _, r, c, k in rows if r == 'assistant' and c.strip() and not k]
        if finals:
            time.sleep(4)  # settle: a final reply after tool calls may still be written
            rows = _turn_rows(peek(user_id)['rows'])
            finals = [c for _, r, c, k in rows if r == 'assistant' and c.strip() and not k]
            tools = [json.loads(k)[0]['function']['name'] for _, r, _, k in rows if r == 'assistant' and k]
            return {'reply': finals[-1], 'seconds': round(time.time() - started), 'tools': tools}
    # Never reached the gateway = transport (Plow/iMessage), not a Watson failure.
    return {'reply': None, 'seconds': round(time.time() - started), 'tools': [], 'timeout': True,
            'transport': user_id is None}


def _turn_rows(rows):
    """Rows of this turn only: stop at the next user message (late deliveries interleave)."""
    turn = []
    for row in rows:
        if row[1] == 'user':
            break
        turn.append(row)
    return turn


_SENT = re.compile(r'Sending response \((\d+) chars\)')


def command(text, timeout=60):
    """Send a slash command; return the length of the reply the gateway sent."""
    log = '/var/lib/hermes/logs/agent.log'
    before = int(_exec('sh', '-c', f'wc -l < {log}').strip())
    started = time.time()
    send(text)
    while time.time() - started < timeout:
        time.sleep(2)
        tail = _exec('sh', '-c', f'tail -n +{before + 1} {log}')
        found = _SENT.findall(tail)
        if found:
            return {'reply_len': int(found[-1]), 'seconds': round(time.time() - started)}
    return {'reply_len': None, 'seconds': round(time.time() - started), 'timeout': True}
