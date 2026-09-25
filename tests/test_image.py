import io
import re
import shlex
import signal
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from watson import githubapp
from watson.cli import main
from watson.workflow import NOTICE

ROOT = Path(__file__).resolve().parents[1]


def settles(condition, within=10):
    end = time.monotonic() + within
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


class ImageTests(unittest.TestCase):
    def test_the_pass_runs_with_a_root_owned_home(self):
        # The agent writes /var/lib/hermes, and `gh` reads $HOME/.config/gh: an
        # http_unix_socket planted there is handed GH_TOKEN on every pass.
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        start = run.index('as_agent() {')
        block = run[start:run.index('\n}', start)]
        self.assertIn('env -i', block)
        self.assertNotIn('GH_TOKEN', block)
        self.assertIn('as_agent GH_TOKEN="$GH_TOKEN" ', run)  # the pass's only credential
        home = re.search(r'\bHOME=(\S+)', block).group(1)
        self.assertFalse(home.startswith('/var/lib/hermes'), home)
        self.assertNotRegex(block, r'\b(XDG_[A-Z_]+|GH_CONFIG_DIR)="?/var/lib/hermes')
        self.assertIn(f'install -d -m 0555 -o root -g root {home}\n', (ROOT / 'Dockerfile').read_text())

    def test_the_github_tokens_live_where_only_root_can_enter(self):
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        docker, compose = (ROOT / 'Dockerfile').read_text(), (ROOT / 'compose.yml').read_text()
        store = re.search(r'^STORE=(\S+)$', run, re.M).group(1)
        self.assertEqual(store, str(githubapp.STORE))
        self.assertIn(f'install -d -m 0700 -o root -g root {store}\n', docker)
        self.assertIn(f'- watson-github:{store}\n', compose)  # a named volume takes that mode on first mount
        self.assertRegex(compose, r'\nvolumes:\n(  \S+:\n)*  watson-github:\n')
        self.assertNotIn('/etc/watson/github', compose)
        # Refused before $AUTH can put a token where the agent can reach it.
        self.assertLess(run.index('s6-setuidgid hermes test -x "$STORE"'), run.index('$AUTH prepare'))
        for name in ('WATSON_GITHUB_CLIENT_ID', 'WATSON_GITHUB_APP_SLUG'):
            self.assertRegex(docker, rf'\nARG {name}=\S+\n')
            self.assertRegex(docker, rf'\nENV .*\b{name}=\${name}\b')

    def test_the_pass_gets_its_token_from_root(self):
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        code = [line.strip() for line in run.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual([line for line in code if re.search(r'\bcat\b', line)], [])  # no token file read here
        self.assertIn("AUTH='/opt/hermes/.venv/bin/python3 -I -m watson.githubapp'", code)
        self.assertIn('GH_TOKEN=$($AUTH prepare) || GH_TOKEN=', code)
        for line in code:  # whatever runs in the background is $waiter, which the trap stops
            if re.search(r'[^&]&( |$)', line):
                self.assertRegex(line, r' & waiter=\$!')
        # Between passes $AUTH answers the owner; while the agent can enter the store, nothing does.
        loop = code[code.index('while :; do'):]
        self.assertEqual([line for line in loop if line == 'pause' or 'sleep' in line],
                         ['/bin/sleep 600 & waiter=$!; wait $waiter || true', 'pause', 'pause'])

    def test_the_agent_tells_the_owner_of_a_connection_without_the_token(self):
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        code = [line.strip() for line in run.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        announce = 'as_agent /opt/hermes/.venv/bin/watson --home "$WATSON_HOME" github announce >/dev/null'
        # Right after root answers, before the setup check: before init too, and never as root,
        # whose files in the agent's home would lock the agent out of its own database.
        self.assertEqual(code[code.index('GH_TOKEN=$($AUTH prepare) || GH_TOKEN=') + 1], announce)
        functions = run[run.index('as_agent() {'):run.index('\n}\n', run.index('cycle() {')) + 3]
        with TemporaryDirectory() as tmp:
            setuid, watson = Path(tmp) / 's6-setuidgid', Path(tmp) / 'watson'
            setuid.write_text('#!/bin/sh\necho "uid $1"\nshift\nexec "$@"\n')
            watson.write_text('#!/bin/sh\necho "$*"\nenv | sort\n')
            setuid.chmod(0o755)
            watson.chmod(0o755)
            script = (functions + 'WATSON_HOME=/w GH_TOKEN=ghu_PASS PLOW_AGENT_TOKEN=plow\ncycle\necho ---\n'
                      + announce.replace('>/dev/null', '') + '\n')
            out = subprocess.run(['sh', '-c', script.replace('/command/s6-setuidgid', str(setuid))
                                  .replace('/opt/hermes/.venv/bin/watson', str(watson))],
                                 capture_output=True, text=True, check=True, timeout=10).stdout
        passes, told = (part.splitlines() for part in out.split('---\n'))
        self.assertEqual(passes[:2], ['uid hermes', '--home /w cycle'])
        self.assertIn('GH_TOKEN=ghu_PASS', passes)
        self.assertEqual(told[:2], ['uid hermes', '--home /w github announce'])
        self.assertIn('PLOW_AGENT_TOKEN=plow', told)
        self.assertEqual([line for line in told if 'ghu_' in line or line.startswith('GH_TOKEN')], [])

    def test_shutdown_stops_the_waiter_and_a_failing_auth_never_spins(self):
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        # The trap and the pause exactly as the loop runs them, with the ten minutes made short.
        prelude = run[run.index("trap '"):run.index('while :; do')].replace(' 600', ' 0.5')
        with TemporaryDirectory() as tmp:
            auth, out = Path(tmp) / 'auth', Path(tmp) / 'auth.out'
            auth.write_text(f"#!/bin/sh\ntrap 'kill $!; echo stopped > {out}; exit 0' TERM\n"
                            f"echo started > {out}\nsleep 30 & wait\n")
            auth.chmod(0o755)
            loop = subprocess.Popen(['sh', '-c', f'AUTH={auth}\n{prelude}\npause\n'])
            self.addCleanup(loop.kill)
            self.assertTrue(settles(lambda: out.exists()))
            loop.send_signal(signal.SIGTERM)  # s6 signals the run script alone
            self.assertEqual(loop.wait(10), 0)
            self.assertTrue(settles(lambda: out.read_text() == 'stopped\n'))  # not left running behind it
        started = time.monotonic()
        subprocess.run(['sh', '-c', f'AUTH=false\n{prelude}\npause\n'], check=True, timeout=10)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)  # $AUTH failing at once still pauses

    def test_every_command_the_skill_names_parses(self):
        # The chat runs these verbatim. One the CLI does not know ends the
        # owner's setup on an argparse usage line.
        skill = (ROOT / 'image/skills/watson-setup/SKILL.md').read_text()
        home = re.search(r'^WATSON_HOME=(\S+)$', (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text(),
                         re.M).group(1)
        blocks = ''.join(re.findall(r'^```\w*\n(.*?)^```', skill, re.M | re.S)).replace('\\\n', '')
        commands = [shlex.split(line) for line in blocks.splitlines() if line.startswith('/opt/hermes/.venv/bin/watson ')]
        self.assertTrue(all(c[1:3] == ['--home', home] for c in commands), commands)  # the service's home
        named = {' '.join(c[3:5]) if c[3] == 'github' else c[3] for c in commands}
        self.assertLessEqual({'status', 'github connect', 'init', 'track', 'untrack', 'config', 'show'}, named)
        self.assertTrue(any(c[3] == 'init' and {'--notify-owner', '--language'} <= set(c) for c in commands))
        fill = {'OWNER/REPO': 'demo/repo', 'LOGIN': 'demo-owner', 'LANG': 'pt', 'RUN_ID': '1'}
        for command in commands:
            with self.subTest(' '.join(command[3:])), TemporaryDirectory() as tmp, \
                    mock.patch('watson.cli.connect', return_value={'state': 'pending'}), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    main(['--home', tmp] + [fill.get(word, word) for word in command[3:]])
                except SystemExit as exc:
                    self.fail(f'exit {exc.code}')

    def test_watson_is_read_before_the_platform_and_the_skill_says_when_to_open(self):
        # Appended after the base's "You are a Plow assistant", the persona was
        # ignored: the first boot pitched Mac errands and never ran status.
        docker, persona = (ROOT / 'Dockerfile').read_text(), (ROOT / 'image/persona.md').read_text()
        self.assertIn('cat /tmp/watson-persona.md /opt/hermes/plow-seed/SOUL.md > ', docker)
        self.assertNotIn('/opt/hermes/plow-seed/persona.md', docker)  # nothing appended after the base
        self.assertTrue(persona.startswith('# Watson\n\nYou are Watson.'))
        self.assertIn('/opt/hermes/.venv/bin/watson --home /var/lib/hermes/watson status', persona)
        # Hermes shows a skill's description cut to its first ~57 characters.
        skill = (ROOT / 'image/skills/watson-setup/SKILL.md').read_text()
        head = re.search(r'^description: (.*)$', skill, re.M).group(1)[:57]
        self.assertIn('greeting', head)

    def test_nothing_the_owner_reads_is_a_markdown_list(self):
        # Plow runs a "- " or "1. " list into one block and glues the next
        # paragraph onto its last item: the number goes inside the bold.
        persona = (ROOT / 'image/persona.md').read_text()
        self.assertIn('Never start a line with - or 1. in chat', persona)
        self.assertIn('**1. GitHub**', persona)
        skill = (ROOT / 'image/skills/watson-setup/SKILL.md').read_text()
        templates = [line[2:] for line in skill.splitlines() if line.startswith('> ')]
        self.assertTrue(templates)
        texts = [t for words in NOTICE.values() for t in words.values() if isinstance(t, str)]
        texts += [t for words in NOTICE.values() for t in words['refused'].values()]
        for text in templates + texts:
            self.assertNotRegex(text, r'(?m)^\s*([-*+]|\d+\.) ', text)


if __name__ == '__main__':
    unittest.main()
