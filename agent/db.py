"""The local SQLite database: synced lotteries, plus usage counters.

Agent tools read lotteries only from here (SPEC.md GROUND-6); the sync job
(sync/lotteries.py) is the only writer of lottery rows.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lotteries (
    lottery_id      INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    boroughs        TEXT NOT NULL,          -- JSON list, from building ZIP codes
    neighborhood    TEXT,
    addresses       TEXT NOT NULL,          -- JSON list
    start_date      TEXT,
    end_date        TEXT NOT NULL,
    is_mini         INTEGER NOT NULL DEFAULT 0,
    is_mitchell_lama INTEGER NOT NULL DEFAULT 0,
    has_income_table INTEGER NOT NULL DEFAULT 0,
    set_asides      TEXT NOT NULL DEFAULT '[]',  -- JSON list of {name, percent}
    in_latest_sync  INTEGER NOT NULL DEFAULT 1,
    source_mode     TEXT NOT NULL DEFAULT 'api'  -- api | fallback
);
CREATE TABLE IF NOT EXISTS income_rows (
    lottery_id      INTEGER NOT NULL REFERENCES lotteries(lottery_id),
    tier            TEXT NOT NULL,
    unit_size       TEXT NOT NULL,
    monthly_rent    REAL NOT NULL,
    units_available INTEGER NOT NULL,
    household_size  INTEGER NOT NULL,
    income_min      REAL NOT NULL,
    income_max      REAL NOT NULL,
    asset_limit     REAL
);
CREATE INDEX IF NOT EXISTS income_rows_lottery ON income_rows(lottery_id);
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_spend (day TEXT PRIMARY KEY, usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_usage (session_id TEXT PRIMARY KEY, messages INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ip_usage (ip TEXT NOT NULL, hour TEXT NOT NULL, messages INTEGER NOT NULL,
                                     PRIMARY KEY (ip, hour));
"""


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO sync_meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def lottery_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("boroughs", "addresses", "set_asides"):
        d[key] = json.loads(d[key])
    for key in ("is_mini", "is_mitchell_lama", "has_income_table", "in_latest_sync"):
        d[key] = bool(d[key])
    return d
