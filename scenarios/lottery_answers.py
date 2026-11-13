"""Compute expected lottery answers from a frozen fixture.

The expected outcome of a lottery scenario must come from the data, not from
anyone's reading of it (scenarios/README.md, invariant 1). This module applies
OPEN-1 and the per-household income ranges to a fixture written by
capture_lottery_fixture.py.

    uv run python -m scenarios.lottery_answers Brooklyn 2 65000
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(as_of: str) -> dict:
    return json.loads((FIXTURES / f"lotteries-{as_of}.json").read_text())


def _money(text: str) -> float:
    return float(text.replace("$", "").replace(",", ""))


def income_rows(income_table: list[dict]):
    """Yield one row per (tier, unit size, household size) from the API's HTML-table strings."""
    for tier in income_table:
        for unit in tier["unitTypes"]:
            sizes = [int(n) for n in re.findall(r"(\d+)\s*people", unit["householdSize"])]
            ranges = re.findall(r"\$([\d,.]+)\s*-\s*\$([\d,.]+)", unit["annualHouseholdIncome"])
            for size, (lo, hi) in zip(sizes, ranges):
                yield {
                    "tier": tier["ami"].strip(),
                    "unit_size": unit["unitSize"].strip(),
                    "monthly_rent": _money(unit["monthlyRent"]),
                    "units_available": int(unit["unitsAvailable"] or 0),
                    "household_size": size,
                    "income_min": _money(lo),
                    "income_max": _money(hi),
                }


# ZIP prefixes by borough. Building `city` values are unreliable (Queens
# buildings list neighborhoods such as "Far Rockaway"; Manhattan's say
# "New York"), so a "Multiple Borough" lottery is placed by its ZIP codes.
ZIP_BOROUGH = {"100": "Manhattan", "101": "Manhattan", "102": "Manhattan", "103": "Staten Island",
               "104": "Bronx", "112": "Brooklyn", "110": "Queens", "111": "Queens", "113": "Queens",
               "114": "Queens", "116": "Queens"}


def boroughs(lottery: dict) -> set[str]:
    """The boroughs a lottery's buildings are in."""
    label = lottery["search"]["borough"].strip()
    if label != "Multiple Borough":
        return {label}
    zips = {str(b.get("zip") or "")[:3] for b in lottery["advertisement"].get("lotteryBuildings", [])}
    return {ZIP_BOROUGH[z] for z in zips if z in ZIP_BOROUGH}


def is_open(lottery: dict, today: date) -> bool:
    """OPEN-1: in the latest search, end date today or later, rental, not a mini-lottery."""
    end = date.fromisoformat(lottery["search"]["lotteryEndDate"][:10])
    return end >= today and not lottery["advertisement"].get("isMiniLottery")


def matches(
    fixture: dict,
    borough: str,
    household_size: int,
    income: float,
    unknown_ids: frozenset[int] = frozenset(),
) -> list[dict]:
    """Open lotteries in the borough with at least one unit the household qualifies for, soonest end first.

    `unknown_ids` simulates lotteries whose income table has not been synced
    yet: they are returned with eligibility "unknown" and no units (RESP-5).
    """
    today = date.fromisoformat(fixture["as_of"])
    out = []
    for lot in fixture["lotteries"]:
        s = lot["search"]
        if borough.lower() not in {b.lower() for b in boroughs(lot)} or not is_open(lot, today):
            continue
        if s["lotteryId"] in unknown_ids:
            end = date.fromisoformat(s["lotteryEndDate"][:10])
            out.append({"lottery_id": s["lotteryId"], "name": s["lotteryName"].strip(),
                        "end_date": end.isoformat(), "closes_today": end == today,
                        "url": f"https://housingconnect.nyc.gov/PublicWeb/details/{s['lotteryId']}",
                        "eligibility": "unknown", "units": []})
            continue
        units = [
            r for r in income_rows(lot["income_table"])
            if r["household_size"] == household_size and r["income_min"] <= income <= r["income_max"]
        ]
        if units:
            end = date.fromisoformat(s["lotteryEndDate"][:10])
            out.append({
                "lottery_id": s["lotteryId"],
                "name": s["lotteryName"].strip(),
                "end_date": end.isoformat(),
                "closes_today": end == today,
                "url": f"https://housingconnect.nyc.gov/PublicWeb/details/{s['lotteryId']}",
                "eligibility": "eligible",
                "units": units,
            })
    return sorted(out, key=lambda m: (m["end_date"], m["lottery_id"]))


BOROUGH_CODES = {"Bronx": "BX", "Brooklyn": "BK", "Manhattan": "MN", "Queens": "QN", "Staten Island": "SI"}


def fallback_open(as_of: str, borough: str) -> tuple[list[int], str]:
    """Backup mode (GROUND-7): Active rows ending today or later, and the dataset refresh date."""
    data = json.loads((FIXTURES / f"opendata-{as_of}.json").read_text())
    ids = sorted(
        int(r["lottery_id"]) for r in data["rows"]
        if r.get("borough") == BOROUGH_CODES[borough] and r["lottery_end_date"][:10] >= as_of
    )
    return ids, data["rows_updated_at"][:10]


def below_minimum(fixture: dict, borough: str, household_size: int, income: float) -> list[int]:
    """Open lotteries where the household's income is below every minimum for its size.

    The minimum may not apply to applicants with Section 8 or another rental
    subsidy (SPEC.md TOOL-3), so the agent must say so for these.
    """
    today = date.fromisoformat(fixture["as_of"])
    out = []
    for lot in fixture["lotteries"]:
        if borough.lower() not in {b.lower() for b in boroughs(lot)} or not is_open(lot, today):
            continue
        rows = [r for r in income_rows(lot["income_table"]) if r["household_size"] == household_size]
        if rows and all(income < r["income_min"] and income <= r["income_max"] for r in rows):
            out.append(lot["search"]["lotteryId"])
    return sorted(out)


def income_gap(fixture: dict, borough: str, household_size: int, income: float) -> list[int]:
    """Open lotteries Housing Connect's search would show but where no unit fits (SPEC.md RESP-5).

    The income is between the lowest minimum and the highest maximum for the household size,
    but inside no single unit's range.
    """
    today = date.fromisoformat(fixture["as_of"])
    out = []
    for lot in fixture["lotteries"]:
        if borough.lower() not in {b.lower() for b in boroughs(lot)} or not is_open(lot, today):
            continue
        rows = [r for r in income_rows(lot["income_table"]) if r["household_size"] == household_size]
        inside_overall = rows and min(r["income_min"] for r in rows) <= income <= max(r["income_max"] for r in rows)
        if inside_overall and not any(r["income_min"] <= income <= r["income_max"] for r in rows):
            end = lot["search"]["lotteryEndDate"][:10]
            out.append((end, lot["search"]["lotteryId"]))
    return [i for _, i in sorted(out)]


def main(argv: list[str]) -> int:
    borough, size, income = argv[0], int(argv[1]), float(argv[2])
    as_of = argv[3] if len(argv) > 3 else max(p.stem.split("-", 1)[1] for p in FIXTURES.glob("lotteries-*.json"))
    for m in matches(load_fixture(as_of), borough, size, income):
        print(m["lottery_id"], m["end_date"], "CLOSES TODAY" if m["closes_today"] else "", m["name"])
        for u in m["units"]:
            print(f"    {u['unit_size']:<10} {u['tier']:<26} ${u['monthly_rent']:,.0f}/mo  "
                  f"{u['units_available']} units  ${u['income_min']:,.0f}-${u['income_max']:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
