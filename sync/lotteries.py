"""The lottery sync job (SPEC.md GROUND-3, GROUND-6, GROUND-7). Plain code, no model calls.

    uv run python -m sync.lotteries                       # sync from the Housing Connect API
    uv run python -m sync.lotteries --fallback            # sync from the Open Data backup
    uv run python -m sync.lotteries --fixture scenarios/fixtures/lotteries-2026-11-10.json

One search call returns every open lottery; each lottery not yet stored gets
two more calls (advertisement and income table), once. A failed run keeps
the previous data. The server runs this on a schedule (lottery_sync in
facts.yaml); the fixture and fallback modes exist for evals and outages.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi

from agent import db
from agent.config import load_facts

HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
OPEN_DATA = "https://data.cityofnewyork.us"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# The search body must include every field (GROUND-3).
SEARCH_BODY = {
    "UnitTypes": [], "NearbyPlaces": [], "NearbySubways": [], "Amenities": [],
    "Applied": None, "HPDUserId": None, "Boroughs": [], "Neighborhoods": [],
    "HouseholdSize": None, "Income": "", "HouseholdType": 1, "OwnerTypes": [],
    "PreferanceTypes": [], "LotteryTypes": [], "Min": None, "Max": None,
    "RentalSubsidy": None,
}

# Building ZIP prefix -> borough. Building `city` values are unreliable
# (neighborhood names in Queens, "New York" in Manhattan).
ZIP_BOROUGH = {"100": "Manhattan", "101": "Manhattan", "102": "Manhattan", "103": "Staten Island",
               "104": "Bronx", "112": "Brooklyn", "110": "Queens", "111": "Queens", "113": "Queens",
               "114": "Queens", "116": "Queens"}
OPEN_DATA_BOROUGH = {"BX": "Bronx", "BK": "Brooklyn", "MN": "Manhattan", "QN": "Queens", "SI": "Staten Island"}


def _get(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        return json.load(resp)


def _money(text: str | None) -> float | None:
    if not text:
        return None
    return float(re.sub(r"[^\d.]", "", text) or 0)


def parse_income_table(income_table: list[dict]) -> list[dict]:
    """One row per (tier, unit size, household size). Sizes and incomes arrive as HTML table strings."""
    rows = []
    for tier in income_table or []:
        for unit in tier.get("unitTypes") or []:
            sizes = [int(n) for n in re.findall(r"(\d+)\s*people", unit.get("householdSize") or "")]
            ranges = re.findall(r"\$([\d,.]+)\s*-\s*\$([\d,.]+)", unit.get("annualHouseholdIncome") or "")
            for size, (lo, hi) in zip(sizes, ranges):
                rows.append({
                    "tier": tier["ami"].strip(), "unit_size": unit["unitSize"].strip(),
                    "monthly_rent": _money(unit.get("monthlyRent")) or 0.0,
                    "units_available": int(unit.get("unitsAvailable") or 0),
                    "household_size": size, "income_min": _money(lo), "income_max": _money(hi),
                    "asset_limit": _money(unit.get("assetLimit")),
                })
    return rows


def building_boroughs(label: str, buildings: list[dict]) -> list[str]:
    label = label.strip()
    if label in OPEN_DATA_BOROUGH.values():
        return [label]
    found = {ZIP_BOROUGH.get(str(b.get("zip") or "")[:3]) for b in buildings}
    return sorted(b for b in found if b)


def store_lottery(conn: sqlite3.Connection, search: dict, advertisement: dict | None,
                  income_table: list[dict] | None, mode: str = "api") -> None:
    lid = search["lotteryId"]
    buildings = (advertisement or {}).get("lotteryBuildings") or []
    set_asides = [{"name": p.get("name"), "percent": p.get("requirementAllocation")}
                  for p in (advertisement or {}).get("lotterySetAsidePreferences") or []]
    rows = parse_income_table(income_table) if income_table is not None else []
    conn.execute("DELETE FROM income_rows WHERE lottery_id = ?", (lid,))
    conn.execute(
        "INSERT OR REPLACE INTO lotteries(lottery_id, name, boroughs, neighborhood, addresses, start_date,"
        " end_date, is_mini, is_mitchell_lama, has_income_table, set_asides, in_latest_sync, source_mode)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (lid, search["lotteryName"].strip(),
         json.dumps(building_boroughs(search.get("borough") or "", buildings)),
         (search.get("neighborhood") or "").strip() or None,
         json.dumps([b.get("address") for b in buildings if b.get("address")]),
         (search.get("lotteryStartDate") or "")[:10] or None, search["lotteryEndDate"][:10],
         int(bool((advertisement or {}).get("isMiniLottery"))),
         int(bool(search.get("isMitchelLamaLottery") or (advertisement or {}).get("isMitchelLamaLottery"))),
         int(bool(rows)), json.dumps(set_asides), mode))
    conn.executemany(
        "INSERT INTO income_rows VALUES (:lottery_id, :tier, :unit_size, :monthly_rent, :units_available,"
        " :household_size, :income_min, :income_max, :asset_limit)",
        [{**r, "lottery_id": lid} for r in rows])


def _finish(conn: sqlite3.Connection, seen: set[int], mode: str, data_as_of: str) -> None:
    """Mark lotteries missing from this run as no longer open; record the run (GROUND-6 step 3-4)."""
    conn.execute("UPDATE lotteries SET in_latest_sync = 0")
    if seen:
        conn.execute(f"UPDATE lotteries SET in_latest_sync = 1 WHERE lottery_id IN ({','.join('?' * len(seen))})",
                     sorted(seen))
    db.set_meta(conn, "source_mode", mode)
    db.set_meta(conn, "data_as_of", data_as_of)
    db.set_meta(conn, "last_success_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def sync_from_api(conn: sqlite3.Connection) -> int:
    base = load_facts()["lottery_sync"]["api_base_url"]
    search = _get(base + "Lottery/SearchLotteries", SEARCH_BODY)
    stored = {r["lottery_id"] for r in conn.execute(
        "SELECT lottery_id FROM lotteries WHERE has_income_table = 1 AND source_mode = 'api'")}
    seen = set()
    for item in search.get("rentals") or []:
        lid = item["lotteryId"]
        seen.add(lid)
        if lid in stored:
            continue
        adv = _get(f"{base}Lottery/GetLotteryAdvertisement?lotteryId={lid}")
        income = _get(f"{base}LotteryConfig/GetAdvertisement4Rent?lotteryid={lid}")
        store_lottery(conn, item, adv, income)
    _finish(conn, seen, "api", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return len(seen)


def load_fixture(conn: sqlite3.Connection, path: Path, unknown_ids: frozenset[int] = frozenset()) -> int:
    """Load a frozen capture (scenarios/fixtures). `unknown_ids` load without income tables (RESP-5 tests)."""
    data = json.loads(Path(path).read_text())
    conn.execute("DELETE FROM income_rows")
    conn.execute("DELETE FROM lotteries")
    seen = set()
    for lot in data["lotteries"]:
        lid = lot["search"]["lotteryId"]
        seen.add(lid)
        store_lottery(conn, lot["search"], lot["advertisement"],
                      None if lid in unknown_ids else lot["income_table"])
    _finish(conn, seen, "api", data["captured_at"])
    return len(seen)


def sync_from_opendata(conn: sqlite3.Connection, fixture: Path | None = None) -> int:
    """Backup mode (GROUND-7): Active rows ending today or later; no income tables."""
    if fixture:
        data = json.loads(Path(fixture).read_text())
        rows, refreshed = data["rows"], data["rows_updated_at"]
    else:
        meta = _get(f"{OPEN_DATA}/api/views/vy5i-a666.json")
        rows = _get(f"{OPEN_DATA}/resource/vy5i-a666.json?lottery_status=Active&$limit=1000")
        refreshed = datetime.fromtimestamp(meta["rowsUpdatedAt"], timezone.utc).isoformat(timespec="seconds")
    conn.execute("DELETE FROM income_rows")
    conn.execute("DELETE FROM lotteries")
    seen = set()
    for r in rows:
        if r.get("development_type") != "Rental" or r.get("borough") not in OPEN_DATA_BOROUGH:
            continue
        lid = int(r["lottery_id"])
        seen.add(lid)
        conn.execute(
            "INSERT OR REPLACE INTO lotteries(lottery_id, name, boroughs, addresses, start_date, end_date,"
            " has_income_table, in_latest_sync, source_mode) VALUES (?, ?, ?, '[]', ?, ?, 0, 1, 'fallback')",
            (lid, r["lottery_name"].strip(), json.dumps([OPEN_DATA_BOROUGH[r["borough"]]]),
             (r.get("lottery_start_date") or "")[:10] or None, r["lottery_end_date"][:10]))
    _finish(conn, seen, "fallback", refreshed)
    return len(seen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync open rental lotteries into the local database.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fixture", type=Path, help="load a frozen lottery fixture instead of the API")
    group.add_argument("--fallback", action="store_true", help="use the Open Data backup")
    parser.add_argument("--opendata-fixture", type=Path, help="frozen Open Data copy for --fallback")
    args = parser.parse_args(argv)
    with db.connection() as conn:
        if args.fixture:
            n = load_fixture(conn, args.fixture)
        elif args.fallback:
            n = sync_from_opendata(conn, args.opendata_fixture)
        else:
            n = sync_from_api(conn)
        print(f"synced {n} open rental lotteries ({db.get_meta(conn, 'source_mode')}, "
              f"data as of {db.get_meta(conn, 'data_as_of')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
