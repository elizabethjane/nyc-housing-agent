"""Offline checks that the scenario validator catches ungrounded expectations."""

import copy
from pathlib import Path

import pytest

from scenarios import lottery_answers, validate

PILOT = Path(__file__).resolve().parents[1] / "scenarios" / "pilot_scenarios.jsonl"


@pytest.fixture(scope="module")
def pilot():
    return {row["id"]: row for row in validate.load_jsonl(PILOT)}


def test_pilot_validates(pilot):
    assert validate.validate(list(pilot.values())) == []


def test_full_set_is_current_and_valid():
    from scenarios import build_full, critic
    rows = build_full.build()
    on_disk = validate.load_jsonl(PILOT.with_name("full_scenarios.jsonl"))
    assert rows == on_disk, "full_scenarios.jsonl is stale; run: uv run python -m scenarios.build_full"
    assert validate.validate(rows) == []
    assert critic.critique(rows) == []


@pytest.mark.parametrize(
    "rewrite, ok",
    [
        ("so 5 of us, 95k a year, any lotteries in brooklyn?", True),
        ("so 5 of us, 90k a year, any lotteries in brooklyn?", False),   # income changed
        ("so 5 of us, 95k a year, any lotteries in queens?", False),     # borough changed
        ("so 5 of us, 95k a year, any lotteries?", False),               # borough dropped
    ],
)
def test_critic_rewrites_must_keep_every_fact(rewrite, ok):
    from scenarios.apply_reviews import check
    original = {"opening_message": "5 of us, 95k a year, brooklyn please", "followups": []}
    assert (check(original, {"opening_message": rewrite, "followups": []}) is None) == ok


def test_multi_borough_lottery_matches_its_buildings_borough():
    fixture = lottery_answers.load_fixture("2026-11-10")
    ids = [m["lottery_id"] for m in lottery_answers.matches(fixture, "Bronx", 2, 40000)]
    assert 7625 in ids  # labeled "Multiple Borough"; all four buildings are in the Bronx


def test_lottery_matches_respect_household_income_ranges():
    fixture = lottery_answers.load_fixture("2026-11-10")
    ids = [m["lottery_id"] for m in lottery_answers.matches(fixture, "Brooklyn", 2, 65000)]
    # The API's own search also returns 7626 and 7690, which have no units
    # for 2 people at $65,000 (SPEC.md GROUND-3).
    assert ids == [7650, 7718]


def test_income_range_is_inclusive_at_the_minimum():
    fixture = lottery_answers.load_fixture("2026-11-10")
    ids = [m["lottery_id"] for m in lottery_answers.matches(fixture, "Brooklyn", 1, 65418)]
    assert 7675 in ids  # 899 Fulton Street, 1 person minimum $65,418


def _errors_after(pilot, scenario_id, change):
    row = copy.deepcopy(pilot[scenario_id])
    change(row)
    return validate.validate([row])


def test_wrong_lottery_list_is_caught(pilot):
    def change(row):
        row["expected"]["lottery_ids_in_order"] = [7650, 7718, 7626]
    assert _errors_after(pilot, "pilot-001", change)


def test_wrong_percent_is_caught(pilot):
    def change(row):
        row["expected"]["checks"][0]["percent"] = 3  # Order #57's figure, not #58's
    assert _errors_after(pilot, "pilot-003", change)


def test_unapproved_link_is_caught(pilot):
    def change(row):
        row["expected"]["checks"][1]["urls"] = ["https://example.org/rent"]
    assert _errors_after(pilot, "pilot-003", change)


def test_turn_count_must_match_followups(pilot):
    def change(row):
        row["followups"] = []
    assert _errors_after(pilot, "pilot-004", change)
