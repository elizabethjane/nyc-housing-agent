"""Offline tests of the agent's tools, rules, guards, and limits. No model calls."""

from datetime import date

import pytest

from agent import db, guards, limits, rent_rules, tools
from scenarios import lottery_answers, validate
from sync.lotteries import load_fixture, sync_from_opendata
from tests.conftest import FIXTURE, OPENDATA

# --- Lotteries -------------------------------------------------------------------


QUERIES = [(b, size, income) for b in ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")
           for size, income in [(1, 32000), (1, 70000), (1, 90000), (2, 40000), (2, 62606), (2, 65000),
                                (2, 81420), (3, 60000), (3, 85000), (4, 110000), (5, 95000), (6, 120000)]]


@pytest.mark.parametrize("borough, size, income", QUERIES)
def test_agent_eligibility_matches_the_answer_key(world, borough, size, income):
    """The agent's eligibility code and the eval answer key are written separately; they must agree."""
    got = tools.search_lotteries(borough, size, income)
    expected = lottery_answers.matches(lottery_answers.load_fixture("2026-11-10"), borough, size, income)
    assert [l["lottery_id"] for l in got["lotteries"]] == [m["lottery_id"] for m in expected]


def test_lottery_closing_today_is_open_and_marked(world):
    found = {l["lottery_id"]: l for l in tools.search_lotteries("Brooklyn", 1, 70000)["lotteries"]}
    assert found[7675]["closes_today"] and found[7675]["days_left"] == 0


def test_lottery_is_dropped_the_day_after_it_ends(world, monkeypatch):
    monkeypatch.setenv("NYCHA_TODAY", "2026-11-11")
    ids = [l["lottery_id"] for l in tools.search_lotteries("Brooklyn", 1, 70000)["lotteries"]]
    assert 7675 not in ids


def test_lottery_without_income_table_is_unknown_not_eligible(world):
    with db.connection() as conn:
        load_fixture(conn, FIXTURE, unknown_ids=frozenset({7728}))
    found = {l["lottery_id"]: l for l in tools.search_lotteries("Bronx", 3, 85000)["lotteries"]}
    assert found[7728]["eligibility"] == "unknown" and found[7728]["units"] == []


def test_below_minimum_carries_the_subsidy_note(world):
    below = tools.search_lotteries("Brooklyn", 2, 30000)["below_minimum"]
    assert below and all("Section 8" in b["note"] for b in below)


def test_backup_mode_lists_open_rows_with_unknown_eligibility(world):
    with db.connection() as conn:
        sync_from_opendata(conn, OPENDATA)
    r = tools.search_lotteries("Brooklyn", 2, 65000)
    expected, refreshed = lottery_answers.fallback_open("2026-11-10", "Brooklyn")
    assert r["source_mode"] == "fallback" and "month" in r["note"]
    assert sorted(l["lottery_id"] for l in r["lotteries"]) == expected
    assert all(l["eligibility"] == "unknown" for l in r["lotteries"])
    assert r["data_as_of"].startswith(refreshed)


def test_named_closed_lottery_is_not_found_as_open(world):
    assert tools.find_lottery("Snediker")["matches"] == []
    assert tools.get_lottery(7686)["error"] == "not_found"


def test_bad_lottery_arguments_are_rejected(world):
    assert tools.search_lotteries("Yonkers", 2, 50000)["error"] == "invalid_argument"
    assert tools.search_lotteries("Bronx", 0, 50000)["error"] == "invalid_argument"
    assert tools.run_tool("search_lotteries", {"borough": "Bronx"})["error"] == "invalid_argument"


def test_kill_switch_pauses_lottery_tools_only(world, monkeypatch):
    monkeypatch.setenv("NYCHOUSING_KILL_SWITCH", "laws-only")
    assert tools.search_lotteries("Bronx", 1, 90000)["error"] == "paused"
    assert tools.check_rent_rules("not_regulated")["ok"]


# --- Rent rules ------------------------------------------------------------------


@pytest.mark.parametrize("start, years, order, pct", [
    ("2026-09-30", 2, 57, 4.5), ("2026-10-01", 1, 58, 0), ("2027-09-30", 2, 58, 0), ("2026-06-01", 1, 57, 3)])
def test_stabilized_guideline_is_chosen_by_lease_start(start, years, order, pct):
    r = rent_rules.stabilized_guideline(date.fromisoformat(start), years)
    assert (r["order"], r["guideline_percent"]) == (order, pct)


@pytest.mark.parametrize("start", ["2027-11-01", "2024-01-01"])
def test_no_guideline_outside_stored_orders(start):
    assert rent_rules.stabilized_guideline(date.fromisoformat(start), 1)["error"] == "no_order_for_date"


def test_stabilized_lookup_asks_for_missing_facts():
    assert tools.check_rent_rules("rent_stabilized", lease_start_date="2027-01-01")["error"] == "missing_input"


def test_good_cause_standard_and_luxury_limits():
    r = rent_rules.good_cause_standard()
    assert r["local_rent_standard_percent"] == 8.38
    assert "$6,505" in r["exemptions_to_check"]["luxury"] and "not exempt" in r["exemptions_to_check"]["luxury"]


# --- Sources ---------------------------------------------------------------------


def test_source_search_finds_the_deposit_cap():
    hits = tools.search_sources("how much can a landlord charge for a security deposit")["results"]
    assert any(h["source_id"] == "rgb-faq-security-deposits" for h in hits)


def test_source_section_round_trip():
    hit = tools.search_sources("good cause grounds for removal")["results"][0]
    section = tools.get_source_section(hit["source_id"], hit["section_id"])
    assert section["ok"] and section["url"].startswith("https://")


# --- Guards ----------------------------------------------------------------------


def test_links_off_the_allowlist_are_removed():
    text = ("See [Order #58](https://rentguidelinesboard.cityofnewyork.us/2026-27-apartment-loft-order-58/), "
            "[help](http://bit.ly/rent-help-nyc), ![](https://track.example.com/p.gif), "
            "https://nyc-tenant-rights.example.org/renewals and nyc-gov-tenants.com.")
    clean, removed = guards.strip_external_links(text)
    assert "rentguidelinesboard.cityofnewyork.us" in clean
    for bad in ("bit.ly", "track.example.com", "nyc-tenant-rights.example.org", "nyc-gov-tenants.com"):
        assert bad not in clean
    assert len(removed) == 4


def _scenario_openings(intentions):
    rows = validate.load_jsonl(validate.REPO_ROOT / "scenarios" / "full_scenarios.jsonl")
    return [r for r in rows if r["tuple"]["intention"] in intentions]


def test_injection_guard_has_no_false_positives_on_ordinary_questions():
    flagged = [r["id"] for r in _scenario_openings({"neutral"}) if guards.flags_injection(r["opening_message"])]
    assert flagged == []


def test_injection_guard_catches_prompt_extraction():
    for r in _scenario_openings({"prompt_extraction"}):
        assert guards.flags_injection(r["opening_message"]), r["id"]


def test_redaction():
    assert guards.redact("call me at 718-555-0134 or a.b@example.com, ssn 123-45-6789") == \
        "call me at [phone] or [email], ssn [ssn]"


# --- Limits ----------------------------------------------------------------------


def test_message_and_session_limits(world):
    with db.connection() as conn:
        assert limits.check_message(conn, "x" * 2001, "s1") == "message_too_long"
        for _ in range(30):
            assert limits.check_message(conn, "hi", "s2") is None
        assert limits.check_message(conn, "hi", "s2") == "session_limit"


def test_daily_cap(world):
    with db.connection() as conn:
        limits.record_spend(conn, 4.99)
        assert not limits.over_daily_cap(conn) and limits.spend_alert(conn)
        limits.record_spend(conn, 0.02)
        assert limits.check_message(conn, "hi", "s3") == "daily_cap"


def test_cost_uses_model_price_and_worst_case_for_unknown_models():
    usage = type("U", (), {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0})()
    assert limits.cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0)
    assert limits.cost_usd("some-new-model", usage) == pytest.approx(5.0)


def test_housing_connect_asset_limits_match_the_hdc_policy():
    """Each unit's asset limit in the Housing Connect data equals the HDC table for its AMI tier."""
    import re

    from agent.config import load_facts

    table = load_facts()["lottery_assets"]["rental_asset_limit_usd_by_ami_percent"]
    checked = 0
    for lot in lottery_answers.load_fixture("2026-11-10")["lotteries"]:
        for tier in lot["income_table"]:
            m = re.match(r"(\d+)% AREA MEDIAN INCOME", tier["ami"].strip())
            for unit in tier["unitTypes"]:
                if m and unit.get("assetLimit"):
                    assert float(re.sub(r"[^\d.]", "", unit["assetLimit"])) == table[int(m.group(1))]
                    checked += 1
    assert checked >= 50


def test_source_search_finds_what_counts_toward_assets():
    hits = tools.search_sources("does my 401k retirement account count toward the asset limit")["results"]
    assert hits[0]["source_id"] == "hdc-asset-limits"


@pytest.mark.parametrize("query, expected_source", [
    ("landlord refuse to renew rent stabilized lease", "dhcr-fact-sheet-4"),
    ("when does landlord have to send rent stabilized renewal", "dhcr-fact-sheet-4"),
])
def test_stabilized_renewal_questions_find_the_fact_sheet(query, expected_source):
    assert tools.search_sources(query)["results"][0]["source_id"] == expected_source


def test_hstpa_is_split_by_section_with_plain_law_names():
    from agent.sources import get_index

    titles = {s.section_id: s.title for s in get_index().sections if s.source_id == "hstpa-2019"}
    assert len(titles) > 100
    assert "NYC Rent Stabilization Law" in titles["part-i-s-2"]
    hits = tools.search_sources("rent controlled landlord take apartment for family", source_id="hstpa-2019")
    assert hits["results"][0]["section_id"] in {"part-i-s-1", "part-i-s-4"}


@pytest.mark.parametrize("borough, size, income", QUERIES)
def test_income_gap_matches_the_answer_key(world, borough, size, income):
    """Lotteries Housing Connect's search would show but where no unit fits (RESP-5)."""
    got = [l["lottery_id"] for l in tools.search_lotteries(borough, size, income)["income_gap"]]
    expected = lottery_answers.income_gap(lottery_answers.load_fixture("2026-11-10"), borough, size, income)
    assert got == expected


def test_income_gap_reports_the_nearest_ranges(world):
    gap = {l["lottery_id"]: l for l in tools.search_lotteries("Brooklyn", 2, 65000)["income_gap"]}
    assert set(gap) == {7626, 7690}
    ranges = [(r["income_min"], r["income_max"]) for r in gap[7690]["ranges"]]
    assert (37989.0, 54280.0) in ranges and (75703.0, 108560.0) in ranges and "no unit" in gap[7690]["note"]
