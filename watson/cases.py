"""Durable per-issue workflow state, separate from immutable analysis runs."""
import json
from .core import now


class Cases:
    def __init__(self, store):
        self.store = store
        store.db.executescript('''
        CREATE TABLE IF NOT EXISTS cases (
          repo TEXT, number INTEGER, cursor TEXT, state TEXT, data TEXT, updated TEXT,
          PRIMARY KEY(repo,number));
        CREATE TABLE IF NOT EXISTS validations (
          id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, sha TEXT, result TEXT, created TEXT);
        ''')

    def get(self, repo, number):
        row = self.store.db.execute('SELECT * FROM cases WHERE repo=? AND number=?', (repo,number)).fetchone()
        return dict(row, data=json.loads(row['data'])) if row else None

    def save(self, repo, number, cursor, state, data):
        self.store.db.execute('''INSERT INTO cases VALUES(?,?,?,?,?,?) ON CONFLICT(repo,number)
          DO UPDATE SET cursor=excluded.cursor,state=excluded.state,data=excluded.data,updated=excluded.updated''',
          (repo,number,cursor,state,json.dumps(data,ensure_ascii=False),now()))
        self.store.db.commit()

    def validation(self, repo, number, sha, result):
        self.store.db.execute('INSERT INTO validations(repo,number,sha,result,created) VALUES(?,?,?,?,?)',
                             (repo,number,sha,json.dumps(result,ensure_ascii=False),now()))
        self.store.db.commit()

    def history(self, repo, number):
        return [json.loads(x['result']) for x in self.store.db.execute(
            'SELECT result FROM validations WHERE repo=? AND number=? ORDER BY id DESC LIMIT 5', (repo,number))]
