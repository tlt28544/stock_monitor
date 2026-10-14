import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .models import BudgetError, utcnow

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (report_date TEXT, version TEXT, fingerprint TEXT, demo INTEGER, PRIMARY KEY(report_date,version));
CREATE TABLE IF NOT EXISTS jobs (
 ticker TEXT, report_date TEXT, version TEXT, status TEXT NOT NULL,
 stage TEXT NOT NULL DEFAULT 'pending', error TEXT, error_type TEXT,
 warnings TEXT NOT NULL DEFAULT '[]', artifacts TEXT, updated_at TEXT,
 PRIMARY KEY(ticker,report_date,version));
CREATE TABLE IF NOT EXISTS checkpoints (
 ticker TEXT, report_date TEXT, version TEXT, stage TEXT, payload TEXT,
 PRIMARY KEY(ticker,report_date,version,stage));
CREATE TABLE IF NOT EXISTS articles (
 ticker TEXT, provider TEXT, article_id TEXT, published_at TEXT, retrieved_at TEXT,
 payload TEXT, PRIMARY KEY(ticker,provider,article_id));
CREATE TABLE IF NOT EXISTS events (
 ticker TEXT, event_id TEXT, updated_at TEXT, payload TEXT,
 PRIMARY KEY(ticker,event_id));
CREATE TABLE IF NOT EXISTS ingestion (
 ticker TEXT PRIMARY KEY, through_date TEXT, full_date TEXT);
CREATE TABLE IF NOT EXISTS history (
 id INTEGER PRIMARY KEY, ticker TEXT, report_date TEXT, version TEXT,
 stage TEXT, status TEXT, details TEXT, timestamp TEXT);
CREATE TABLE IF NOT EXISTS valuation_reference (
 ticker TEXT, revision INTEGER, analyst TEXT, approved INTEGER,
 payload TEXT, created_at TEXT, PRIMARY KEY(ticker,revision));
CREATE TABLE IF NOT EXISTS api_ledger (
 id INTEGER PRIMARY KEY, report_date TEXT, version TEXT, ticker TEXT,
 operation TEXT, tokens INTEGER, cost REAL, status TEXT, timestamp TEXT);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(str(self.path), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=30000')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def jobs(self, date, version):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM jobs WHERE report_date=? AND version=? ORDER BY ticker', (date, version))]

    def ensure(self, ticker, date, version):
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO jobs(ticker,report_date,version,status,updated_at) VALUES(?,?,?,?,?)', (ticker,date,version,'pending',utcnow()))

    def mark(self, ticker, date, version, status, stage, error=None, warnings=None, artifacts=None, error_type=None):
        with self.connect() as c:
            c.execute('UPDATE jobs SET status=?,stage=?,error=?,error_type=?,warnings=?,artifacts=COALESCE(?,artifacts),updated_at=? WHERE ticker=? AND report_date=? AND version=?',
                      (status,stage,error,error_type,json.dumps(warnings or []),json.dumps(artifacts) if artifacts else None,utcnow(),ticker,date,version))
            c.execute('INSERT INTO history(ticker,report_date,version,stage,status,details,timestamp) VALUES(?,?,?,?,?,?,?)',
                      (ticker,date,version,stage,status,json.dumps({'error':error,'error_type':error_type}),utcnow()))

    def checkpoint(self, ticker, date, version, stage, payload):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)', (ticker,date,version,stage,json.dumps(payload)))

    def cached(self, ticker, date, version, stage):
        with self.connect() as c:
            r = c.execute('SELECT payload FROM checkpoints WHERE ticker=? AND report_date=? AND version=? AND stage=?',(ticker,date,version,stage)).fetchone()
            return json.loads(r[0]) if r else None

    def ingestion(self, ticker):
        with self.connect() as c:
            r = c.execute('SELECT * FROM ingestion WHERE ticker=?', (ticker,)).fetchone()
            return dict(r) if r else None

    def ingest(self, ticker, articles, through, full, coverage_complete=True):
        with self.connect() as c:
            for a in articles:
                c.execute('INSERT OR REPLACE INTO articles VALUES(?,?,?,?,?,?)', (ticker,a.provider,a.article_id,a.published_at.isoformat(),a.retrieved_at.isoformat(),a.model_dump_json()))
            c.execute('INSERT INTO ingestion VALUES(?,?,?) ON CONFLICT(ticker) DO UPDATE SET through_date=excluded.through_date,full_date=COALESCE(excluded.full_date,ingestion.full_date)', (ticker,through,through if full and coverage_complete else None))

    def articles(self, ticker, start, end):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM articles WHERE ticker=? AND published_at>=? AND published_at<? ORDER BY published_at', (ticker,start.isoformat(),end.isoformat()))]

    def save_events(self, ticker, events):
        with self.connect() as c:
            for e in events:
                c.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?)',(ticker,e['id'],utcnow(),json.dumps(e)))

    def reference(self, ticker):
        with self.connect() as c:
            r = c.execute('SELECT payload,revision,analyst,approved FROM valuation_reference WHERE ticker=? ORDER BY revision DESC LIMIT 1', (ticker,)).fetchone()
            return {**json.loads(r['payload']), 'revision':r['revision'], 'analyst':r['analyst'], 'approved':bool(r['approved'])} if r else None

    def import_reference(self, ticker, analyst, approved, payload):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            revision = c.execute('SELECT COALESCE(MAX(revision),0)+1 FROM valuation_reference WHERE ticker=?',(ticker,)).fetchone()[0]
            c.execute('INSERT INTO valuation_reference VALUES(?,?,?,?,?,?)',(ticker,revision,analyst,int(approved),json.dumps(payload),utcnow()))
            return revision

    def reserve(self, date, version, ticker, operation, tokens, cost, settings):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            used = c.execute('SELECT COALESCE(SUM(tokens),0),COALESCE(SUM(cost),0) FROM api_ledger WHERE report_date=? AND version=?',(date,version)).fetchone()
            if used[0]+tokens > settings.max_tokens or used[1]+cost > settings.max_cost_usd:
                raise BudgetError('Persistent run API budget exhausted')
            return c.execute('INSERT INTO api_ledger(report_date,version,ticker,operation,tokens,cost,status,timestamp) VALUES(?,?,?,?,?,?,?,?)', (date,version,ticker,operation,tokens,cost,'reserved',utcnow())).lastrowid

    def settle(self, ledger_id, tokens, cost):
        with self.connect() as c:
            c.execute("UPDATE api_ledger SET tokens=?,cost=?,status='settled' WHERE id=?",(tokens,cost,ledger_id))

    def usage(self, date, version):
        with self.connect() as c:
            r = c.execute('SELECT COALESCE(SUM(tokens),0),COALESCE(SUM(cost),0) FROM api_ledger WHERE report_date=? AND version=?',(date,version)).fetchone()
            return {'tokens':r[0], 'cost_usd':r[1]}

    def bind_run(self, date, version, fingerprint, demo):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT fingerprint FROM runs WHERE report_date=? AND version=?',(date,version)).fetchone()
            if row and row[0]!=fingerprint:
                raise ValueError('Run configuration differs from persisted checkpoints; use a new version')
            c.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?)',(date,version,fingerprint,int(demo)))

    def run_mode(self, date, version):
        with self.connect() as c:
            row = c.execute('SELECT demo FROM runs WHERE report_date=? AND version=?',(date,version)).fetchone()
            return bool(row[0]) if row else None
