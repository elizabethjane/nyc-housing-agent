"""Which lotteries are open, and who qualifies (SPEC.md OPEN-1 to OPEN-3, TOOL-3, RESP-5).

Deterministic code makes these decisions so the model cannot list a closed
lottery or call someone eligible from a misread table. This module is
written independently of scenarios/lottery_answers.py (the eval answer key);
tests/test_agent_tools.py checks that the two agree.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from agent import db
from agent.config import load_facts

BOROUGHS = ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")


def normalize_borough(name: str) -> str | None:
    key = name.strip().lower().replace("the ", "")
    aliases = {"bk": "Brooklyn", "bx": "Bronx", "si": "Staten Island", "nyc": None,
               "new york": "Manhattan", "staten": "Staten Island"}
    if key in aliases:
        return aliases[key]
    return next((b for b in BOROUGHS if b.lower() == key), None)


def is_open(lottery: dict, today: date) -> bool:
    """OPEN-1: in the latest sync, end date today or later (New York time), not a mini-lottery.
    Rentals only is enforced by the sync job, which stores rentals only."""
    return lottery["in_latest_sync"] and date.fromisoformat(lottery["end_date"]) >= today and not lottery["is_mini"]


def property_url(lottery_id: int) -> str:
    return load_facts()["lottery_sync"]["property_url_template"].format(lottery_id=lottery_id)


def open_lotteries(conn: sqlite3.Connection, today: date, borough: str | None = None) -> list[dict]:
    rows = [db.lottery_row(r) for r in conn.execute("SELECT * FROM lotteries")]
    out = [r for r in rows if is_open(r, today) and (borough is None or borough in r["boroughs"])]
    return sorted(out, key=lambda r: (r["end_date"], r["lottery_id"]))


def income_rows(conn: sqlite3.Connection, lottery_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM income_rows WHERE lottery_id = ? ORDER BY monthly_rent, unit_size", (lottery_id,))]


def assess(conn: sqlite3.Connection, lottery: dict, household_size: int, income: float) -> dict:
    """Eligibility for one lottery: eligible (with units), below_minimum, not_eligible, or unknown."""
    if not lottery["has_income_table"]:
        return {"eligibility": "unknown", "units": []}
    rows = [r for r in income_rows(conn, lottery["lottery_id"]) if r["household_size"] == household_size]
    units = [r for r in rows if r["income_min"] <= income <= r["income_max"]]
    if units:
        return {"eligibility": "eligible", "units": [
            {k: u[k] for k in ("unit_size", "tier", "monthly_rent", "units_available", "income_min",
                               "income_max", "asset_limit")} for u in units]}
    if rows and all(income < r["income_min"] for r in rows):
        lowest = min(rows, key=lambda r: r["income_min"])
        return {"eligibility": "below_minimum", "units": [], "lowest_minimum_income": lowest["income_min"],
                "note": ("Income is below this lottery's minimum for the household size. The minimum may not "
                         "apply to applicants with Section 8 or another rental subsidy.")}
    return {"eligibility": "not_eligible", "units": []}
