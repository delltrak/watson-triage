from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class WatsonError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise WatsonError("Repositório inválido; use owner/repo.")
    return value


def safe_source(path):
    parts = Path(path).parts
    return (bool(parts) and not path.startswith('/') and '..' not in parts
            and not any(p.startswith('.') for p in parts)
            and not any(p.lower() in {'node_modules', 'vendor', 'dist', 'build'} for p in parts)
            and not re.search(r"(?i)(secret|credential|private.?key|auth\.json|\.pem$|\.key$|lock\.)", path)
            and Path(path).suffix.lower() in {
                '.py', '.go', '.ts', '.tsx', '.js', '.jsx', '.php', '.rs', '.java',
                '.css', '.html', '.sql', '.md', '.yaml', '.yml', '.toml', '.json', '.sh'
            })


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
    temporary.replace(path)


def load_config(home):
    try:
        config = json.loads((home / 'config.json').read_text())
    except FileNotFoundError:
        raise WatsonError('Execute watson init primeiro.') from None
    repo_name(config['repository'])
    for repo in config.get('related_repositories', []):
        repo_name(repo)
    return config


class Store:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / 'memory.sqlite', timeout=30)
        os.chmod(self.home / 'memory.sqlite', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS issues (
              repo TEXT, number INTEGER, title TEXT, observed TEXT, tracked INTEGER DEFAULT 0,
              changed INTEGER DEFAULT 0, PRIMARY KEY(repo,number));
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, fingerprint TEXT,
              started TEXT, completed TEXT, status TEXT, result TEXT, error TEXT,
              UNIQUE(repo,number,fingerprint));
            CREATE TABLE IF NOT EXISTS actions (
              key TEXT PRIMARY KEY, run_id INTEGER, kind TEXT, status TEXT, created TEXT,
              result TEXT);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(issues)')}
        if 'checked' not in columns:
            self.db.execute('ALTER TABLE issues ADD COLUMN checked TEXT')
        if 'assigned' not in columns:
            self.db.execute('ALTER TABLE issues ADD COLUMN assigned INTEGER DEFAULT 0')
            self.db.execute('UPDATE issues SET assigned=1')
        self.db.commit()

    def checked(self, repo, number):
        self.db.execute('UPDATE issues SET checked=?,changed=0 WHERE repo=? AND number=?', (now(), repo, number))
        self.db.commit()

    @contextmanager
    def lock(self):
        # A process crash releases the OS lock. Runs left running become resumable.
        import fcntl
        with open(self.home / 'worker.lock', 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WatsonError('Outra triagem do Watson está em execução.') from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def observe(self, repo, issue):
        number = issue['number']
        observed = digest({k: issue.get(k) for k in ('updated_at', 'state', 'assignees')})
        old = self.db.execute('SELECT * FROM issues WHERE repo=? AND number=?', (repo, number)).fetchone()
        if old is None:
            self.db.execute('INSERT INTO issues(repo,number,title,observed) VALUES(?,?,?,?)',
                            (repo, number, issue['title'], observed))
        elif old['observed'] != observed:
            self.db.execute('UPDATE issues SET title=?,observed=?,changed=1 WHERE repo=? AND number=?',
                            (issue['title'], observed, repo, number))
        self.db.commit()

    def track(self, repo, number, enabled=True):
        self.db.execute('''INSERT INTO issues(repo,number,title,tracked,changed) VALUES(?,?,?,?,?)
            ON CONFLICT(repo,number) DO UPDATE SET tracked=excluded.tracked,changed=excluded.changed''',
                        (repo, number, f'#{number}', int(enabled), int(enabled)))
        self.db.commit()

    def latest(self, repo, number):
        row = self.db.execute("SELECT * FROM runs WHERE repo=? AND number=? AND status='complete' ORDER BY id DESC LIMIT 1",
                              (repo, number)).fetchone()
        return dict(row) if row else None

    def begin(self, repo, number, fingerprint):
        row = self.db.execute('SELECT * FROM runs WHERE repo=? AND number=? AND fingerprint=?',
                              (repo, number, fingerprint)).fetchone()
        if row and row['status'] == 'complete':
            return row['id'], False
        if row:
            self.db.execute("UPDATE runs SET status='running',error=NULL,started=? WHERE id=?", (now(), row['id']))
            run_id = row['id']
        else:
            cur = self.db.execute("INSERT INTO runs(repo,number,fingerprint,started,status) VALUES(?,?,?,?,'running')",
                                  (repo, number, fingerprint, now()))
            run_id = cur.lastrowid
        self.db.commit()
        return run_id, True

    def finish(self, run_id, result):
        self.db.execute("UPDATE runs SET status='complete',completed=?,result=?,error=NULL WHERE id=?",
                        (now(), json.dumps(result, ensure_ascii=False), run_id))
        self.db.execute('UPDATE issues SET changed=0 WHERE (repo,number)=(SELECT repo,number FROM runs WHERE id=?)', (run_id,))
        self.db.commit()

    def fail(self, run_id, message):
        self.db.execute("UPDATE runs SET status='failed',error=? WHERE id=?", (message[:500], run_id))
        self.db.commit()

    def run(self, run_id):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise WatsonError('Investigação não encontrada.')
        return dict(row)

    def stage_action(self, run_id, kind, payload):
        """Write the claim WITHOUT committing, so a caller can bind it to
        another write in one transaction. See `claim_action`.
        """
        key = digest({'run': run_id, 'kind': kind, 'payload': payload})
        try:
            self.db.execute("INSERT INTO actions VALUES(?,?,?,'sending',?,NULL)", (key, run_id, kind, now()))
        except sqlite3.IntegrityError:
            raise WatsonError('Esta ação já foi tentada. Confira o histórico antes de reenviar.') from None
        return key

    def claim_action(self, run_id, kind, payload):
        key = self.stage_action(run_id, kind, payload)
        self.db.commit()
        return key

    def action_result(self, key, status, result):
        self.db.execute('UPDATE actions SET status=?,result=? WHERE key=?',
                        (status, json.dumps(result, ensure_ascii=False), key))
        self.db.commit()

    def history(self):
        return {
            'issues': [dict(x) for x in self.db.execute('SELECT * FROM issues ORDER BY tracked DESC,number DESC')],
            'runs': [dict(x) for x in self.db.execute('SELECT id,repo,number,status,started,completed,error FROM runs ORDER BY id DESC LIMIT 30')],
            'actions': [dict(x) for x in self.db.execute('SELECT * FROM actions ORDER BY created DESC LIMIT 30')],
        }
