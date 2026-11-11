"""Freeze today's open rental lotteries as an eval fixture.

Lotteries open and close daily, so lottery scenarios are graded against a
fixed copy of the Housing Connect data rather than the live API. The runner
loads the fixture into the lottery database and freezes the clock at the
fixture's `as_of` date before running a lottery scenario.

    uv run python -m scenarios.capture_lottery_fixture

Writes scenarios/fixtures/lotteries-<today>.json with, for each open rental
lottery, the search result, the advertisement, and the income table
(GROUND-3). Makes one search call plus two calls per lottery.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import certifi

API = "https://a806-housingconnectapi.nyc.gov/HPDPublicAPI/api/"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
OUT_DIR = Path(__file__).resolve().parent / "fixtures"

# The search body must include every field (SPEC.md GROUND-3).
SEARCH_BODY = {
    "UnitTypes": [], "NearbyPlaces": [], "NearbySubways": [], "Amenities": [],
    "Applied": None, "HPDUserId": None, "Boroughs": [], "Neighborhoods": [],
    "HouseholdSize": None, "Income": "", "HouseholdType": 1, "OwnerTypes": [],
    "PreferanceTypes": [], "LotteryTypes": [], "Min": None, "Max": None,
    "RentalSubsidy": None,
}


def call(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


OPEN_DATA = "https://data.cityofnewyork.us"


def capture_opendata_fallback() -> None:
    """Freeze the fallback dataset (GROUND-7) for backup-mode scenarios.

    Saves every lottery row with status Active plus the dataset's last-refresh
    time, so a scenario can check both the open-lottery rule and the date the
    agent reports.
    """
    # This Python's default store lacks the chain data.cityofnewyork.us uses.
    ctx = ssl.create_default_context(cafile=certifi.where())

    def get(url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            return json.load(resp)

    meta = get(f"{OPEN_DATA}/api/views/vy5i-a666.json")
    rows = get(f"{OPEN_DATA}/resource/vy5i-a666.json?lottery_status=Active&$limit=1000")
    refreshed = datetime.fromtimestamp(meta["rowsUpdatedAt"], timezone.utc).isoformat(timespec="seconds")
    out = OUT_DIR / f"opendata-{date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "as_of": date.today().isoformat(),
        "source_id": "hc-lotteries-by-lottery",
        "rows_updated_at": refreshed,
        "rows": rows,
    }, indent=1))
    print(f"wrote {out.name}: {len(rows)} Active rows, dataset refreshed {refreshed}")


def main() -> int:
    search = call("Lottery/SearchLotteries", SEARCH_BODY)
    lotteries = []
    for item in search.get("rentals") or []:
        lid = item["lotteryId"]
        lotteries.append({
            "search": item,
            "advertisement": call(f"Lottery/GetLotteryAdvertisement?lotteryId={lid}"),
            "income_table": call(f"LotteryConfig/GetAdvertisement4Rent?lotteryid={lid}"),
        })
    OUT_DIR.mkdir(exist_ok=True)
    capture_opendata_fallback()
    out = OUT_DIR / f"lotteries-{date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "as_of": date.today().isoformat(),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_id": "housing-connect-api",
        "lotteries": lotteries,
    }, indent=1))
    print(f"wrote {out.name}: {len(lotteries)} open rental lotteries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
