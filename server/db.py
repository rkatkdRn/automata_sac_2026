"""Small portable DB adapter. All state-changing transactions share one DB lock.

SQLite is strictly local. Vercel deployments require PostgreSQL; /tmp is never a
persistence fallback. PostgreSQL transaction advisory locks also work with pooled
connections because the lock ends with the transaction.
"""
from __future__ import annotations
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOCK_ID = 1010502026
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()
_WRITE_LOCKS: dict[str, threading.Lock] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, student_id TEXT UNIQUE, real_name TEXT,
 nickname TEXT NOT NULL UNIQUE, name_hidden INTEGER NOT NULL DEFAULT 0,
 consent_at TEXT, consent_version TEXT NOT NULL DEFAULT '2026-10-cloud-v1'
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, participant_id TEXT, role TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_identities (
 google_sub TEXT PRIMARY KEY, participant_id TEXT NOT NULL UNIQUE,
 school_email TEXT NOT NULL UNIQUE, verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_login_challenges (
 cookie_hash TEXT PRIMARY KEY, nonce_hash TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS google_challenge_expiry ON google_login_challenges(expires_at);
CREATE TABLE IF NOT EXISTS submissions (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, participant_id TEXT,
 day_no INTEGER NOT NULL, attempt_no INTEGER,
 l1 INTEGER NOT NULL CHECK(l1>=0), l2 INTEGER NOT NULL CHECK(l2>=0),
 l3 INTEGER NOT NULL CHECK(l3>=0), l4 INTEGER NOT NULL CHECK(l4>=0), l5 INTEGER NOT NULL CHECK(l5>=0),
 is_seed INTEGER NOT NULL DEFAULT 0, is_ranked INTEGER NOT NULL DEFAULT 1,
 points INTEGER NOT NULL DEFAULT 0, opponents INTEGER NOT NULL DEFAULT 0,
 threshold_bp_used INTEGER NOT NULL DEFAULT 0, submitted_at TEXT NOT NULL,
 response_json TEXT, tag TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
 CHECK(l1+l2+l3+l4+l5=101), UNIQUE(participant_id,day_no,attempt_no)
);
CREATE INDEX IF NOT EXISTS submissions_participant ON submissions(participant_id,day_no);
CREATE TABLE IF NOT EXISTS entitlements (
 id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, premium_exhausted INTEGER NOT NULL DEFAULT 0, choice TEXT,
 status TEXT NOT NULL DEFAULT 'ISSUED', issued_at TEXT NOT NULL, claimed_at TEXT
);
CREATE TABLE IF NOT EXISTS day_config (
 day_no INTEGER PRIMARY KEY, date TEXT NOT NULL, sessions_json TEXT NOT NULL,
 mychew_cap_cum INTEGER NOT NULL, premium_cap_cum INTEGER NOT NULL,
 close_rule TEXT NOT NULL DEFAULT 'EITHER', booth_state TEXT NOT NULL DEFAULT 'CLOSED',
 ever_opened INTEGER NOT NULL DEFAULT 0, threshold_applied INTEGER NOT NULL DEFAULT 0,
 cap_override INTEGER NOT NULL DEFAULT 0, cap_closed_at TEXT, frozen_at TEXT
);
CREATE TABLE IF NOT EXISTS threshold_history (
 id TEXT PRIMARY KEY, day_no INTEGER NOT NULL, value_bp INTEGER NOT NULL,
 source TEXT NOT NULL, reason TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_ledger (
 id TEXT PRIMARY KEY, item TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
 entitlement_id TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rank_freeze (
 scope TEXT PRIMARY KEY, rankings_json TEXT NOT NULL, frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
 id TEXT PRIMARY KEY, action TEXT NOT NULL, reason TEXT NOT NULL, details_json TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, window_start DOUBLE PRECISION NOT NULL, count INTEGER NOT NULL);
"""


def now() -> datetime:
    return datetime.now(ZoneInfo('Asia/Seoul'))


def iso() -> str:
    return now().isoformat(timespec='microseconds')


def target() -> tuple[str, bool]:
    url = os.getenv('DATABASE_URL', '').strip()
    if url:
        if not url.startswith(('postgres://','postgresql://')):
            raise RuntimeError('DATABASE_URL must use PostgreSQL')
        return url, True
    if os.getenv('VERCEL'):
        raise RuntimeError('DATABASE_URL must be configured on Vercel')
    return os.getenv('DB_PATH', 'data/blotto.db'), False


class Connection:
    def __init__(self, raw, postgres):
        self.raw, self.postgres = raw, postgres

    def execute(self, sql, params=()):
        return self.raw.execute(sql.replace('?', '%s') if self.postgres else sql, params)

    def one(self, sql, params=()):
        cursor = self.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        if self.postgres:
            return dict(row)
        # sqlite3.Row looks up named columns linearly. Convert by position once.
        return dict(zip((column[0] for column in cursor.description), row))

    def all(self, sql, params=()):
        cursor = self.execute(sql, params)
        rows = cursor.fetchall()
        if self.postgres:
            return [dict(row) for row in rows]
        columns = tuple(column[0] for column in cursor.description)
        return [dict(zip(columns, row)) for row in rows]

    def scalar(self, sql, params=(), default=0):
        row = self.one(sql, params)
        return next(iter(row.values())) if row else default


def _connect():
    name, postgres = target()
    if postgres:
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(name, connect_timeout=10, row_factory=dict_row, prepare_threshold=None)
    else:
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(name, timeout=30, check_same_thread=False, isolation_level=None)
        raw.row_factory = sqlite3.Row
        try: os.chmod(name, 0o600)
        except OSError: pass
        raw.execute('PRAGMA busy_timeout=30000')
        raw.execute('PRAGMA foreign_keys=ON')
    return Connection(raw, postgres)


def initialize(force=False):
    name, postgres = target()
    with _INIT_LOCK:
        if name in _INITIALIZED and not force:
            return
        conn = _connect()
        try:
            if postgres:
                conn.execute('SELECT pg_advisory_xact_lock(?)', (LOCK_ID,))
            else:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('BEGIN IMMEDIATE')
            for stmt in SCHEMA.split(';'):
                if stmt.strip(): conn.execute(stmt)
            for day, date, chew, premium in [(1,'2026-10-30',188,40),(2,'2026-10-31',338,72),(3,'2026-11-01',450,95)]:
                conn.execute('INSERT INTO day_config(day_no,date,sessions_json,mychew_cap_cum,premium_cap_cum) VALUES(?,?,?,?,?) ON CONFLICT(day_no) DO NOTHING', (day,date,'[["10:00","13:00"],["14:00","17:00"]]',chew,premium))
            for key,value in [('active_day','1'),('version','0'),('score_cache','{}'),('board_cache','{}'),('cycle','null'),('cycle_at',''),('purged','false')]:
                conn.execute('INSERT INTO app_config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',(key,value))
            conn.raw.commit()
            _INITIALIZED.add(name)
        except BaseException:
            conn.raw.rollback()
            raise
        finally:
            conn.raw.close()


@contextmanager
def transaction(write=False):
    initialize()
    name, postgres = target()
    with _INIT_LOCK:
        gate = _WRITE_LOCKS.setdefault(name, threading.Lock())
    if write and not postgres: gate.acquire()
    conn = None
    try:
        conn = _connect()
        if conn.postgres:
            if write: conn.execute('SELECT pg_advisory_xact_lock(?)', (LOCK_ID,))
        else:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield conn
        conn.raw.commit()
    except BaseException:
        if conn: conn.raw.rollback()
        raise
    finally:
        if conn: conn.raw.close()
        if write and not postgres: gate.release()


def config(conn, key, default=None):
    row = conn.one('SELECT value FROM app_config WHERE key=?',(key,))
    return row['value'] if row else default


def set_config(conn, key, value):
    conn.execute('INSERT INTO app_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))


def bump(conn):
    conn.execute("UPDATE app_config SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='version'")
