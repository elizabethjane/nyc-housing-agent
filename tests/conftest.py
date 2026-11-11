"""Test fixtures: a temporary database loaded with the frozen lottery fixture, and a frozen date."""

from pathlib import Path

import pytest

from agent import db
from sync.lotteries import load_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scenarios" / "fixtures" / "lotteries-2026-11-10.json"
OPENDATA = ROOT / "scenarios" / "fixtures" / "opendata-2026-11-10.json"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fresh database with the 2026-11-10 lotteries, today frozen at 2026-11-10, traces in tmp."""
    monkeypatch.setenv("NYCHA_DB", str(tmp_path / "agent.db"))
    monkeypatch.setenv("NYCHA_TODAY", "2026-11-10")
    monkeypatch.setenv("NYCHA_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("NYCHOUSING_KILL_SWITCH", raising=False)
    with db.connection() as conn:
        load_fixture(conn, FIXTURE)
    return tmp_path
