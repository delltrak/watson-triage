import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImageTests(unittest.TestCase):
    def test_the_pass_runs_with_a_root_owned_home(self):
        # The agent writes /var/lib/hermes, and `gh` reads $HOME/.config/gh: an
        # http_unix_socket planted there is handed GH_TOKEN on every pass.
        run = (ROOT / 'image/s6-overlay/s6-rc.d/watson-cycle/run').read_text()
        start = run.index('cycle() {')
        block = run[start:run.index('\n}', start)]
        self.assertIn('env -i', block)
        home = re.search(r'\bHOME=(\S+)', block).group(1)
        self.assertFalse(home.startswith('/var/lib/hermes'), home)
        self.assertNotRegex(block, r'\b(XDG_[A-Z_]+|GH_CONFIG_DIR)="?/var/lib/hermes')
        self.assertIn(f'install -d -m 0555 -o root -g root {home}\n', (ROOT / 'Dockerfile').read_text())


if __name__ == '__main__':
    unittest.main()
