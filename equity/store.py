"""SQLite keeps only jobs and resumable search/narrative checkpoints."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS monthly_runs (
                    report_date TEXT, version TEXT, fingerprint TEXT,
                    PRIMARY KEY(report_date,version));
                CREATE TABLE IF NOT EXISTS monthly_jobs (
                    ticker TEXT, report_date TEXT, version TEXT, status TEXT, stage TEXT,
                    error TEXT, notes TEXT NOT NULL DEFAULT '[]', artifacts TEXT, updated_at TEXT,
                    PRIMARY KEY(ticker,report_date,version));
                CREATE TABLE IF NOT EXISTS monthly_checkpoints (
                    ticker TEXT, report_date TEXT, version TEXT, stage TEXT, payload TEXT,
                    PRIMARY KEY(ticker,report_date,version,stage));
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bind_run(self, report_date, version, fingerprint):
        with self.connect() as conn:
            row = conn.execute('SELECT fingerprint FROM monthly_runs WHERE report_date=? AND version=?',
                               (report_date, version)).fetchone()
            if row and row[0] != fingerprint:
                raise ValueError('Configuration changed; use a new --version to start a fresh run')
            conn.execute('INSERT OR IGNORE INTO monthly_runs VALUES(?,?,?)', (report_date, version, fingerprint))

    def ensure(self, ticker, report_date, version):
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO monthly_jobs(ticker,report_date,version,status,stage) VALUES(?,?,?,?,?)',
                         (ticker, report_date, version, 'pending', 'pending'))

    def jobs(self, report_date, version):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM monthly_jobs WHERE report_date=? AND version=? ORDER BY ticker',
                                                 (report_date, version))]

    def mark(self, ticker, report_date, version, status, stage, error=None, notes=None, artifacts=None):
        with self.connect() as conn:
            conn.execute('UPDATE monthly_jobs SET status=?,stage=?,error=?,notes=?,artifacts=COALESCE(?,artifacts),updated_at=? '
                         'WHERE ticker=? AND report_date=? AND version=?',
                         (status, stage, error, json.dumps(notes or []), json.dumps(artifacts) if artifacts else None,
                          datetime.now(timezone.utc).isoformat(), ticker, report_date, version))

    def cached(self, ticker, report_date, version, stage):
        with self.connect() as conn:
            row = conn.execute('SELECT payload FROM monthly_checkpoints WHERE ticker=? AND report_date=? AND version=? AND stage=?',
                               (ticker, report_date, version, stage)).fetchone()
            return json.loads(row[0]) if row else None

    def checkpoint(self, ticker, report_date, version, stage, payload):
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO monthly_checkpoints VALUES(?,?,?,?,?)',
                         (ticker, report_date, version, stage, json.dumps(payload)))
