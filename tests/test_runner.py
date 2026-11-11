"""The grader and the runner, offline. The runner is driven by the scripted fake model."""

import pytest

from scenarios import grade, runner
from scenarios.validate import REPO_ROOT, load_jsonl
from tests.test_agent_loop import FakeClient, text, tool_call

SCENARIOS = {s["id"]: s for f in ("pilot_scenarios.jsonl", "full_scenarios.jsonl")
             for s in load_jsonl(REPO_ROOT / "scenarios" / f)}
D = "https://housingconnect.nyc.gov/PublicWeb/details/"
OPEN = "https://housingconnect.nyc.gov/PublicWeb/search-lotteries"


def turns(*replies):
    return [{"reply": r, "removed_links": [], "blocked": None, "model_calls": 1} for r in replies]


def test_a_correct_lottery_answer_passes():
    reply = (f"Two open lotteries fit:\n- [292 East 51st Street]({D}7650), closes Nov 16\n"
             f"- [Kingsland Commons Building 2]({D}7718), closes Nov 25\n"
             f"Based on Housing Connect data as of November 10, 2026. [All open lotteries]({OPEN})")
    assert grade.grade(SCENARIOS["pilot-001"], turns(reply))["status"] == "pass"


def test_wrong_order_or_an_extra_lottery_fails():
    reply = f"[A]({D}7718) [B]({D}7650) [C]({D}7626) as of 2026-11-10"
    result = grade.grade(SCENARIOS["pilot-001"], turns(reply))
    failed = {c["check"] for c in result["checks"] if c["status"] == "fail"}
    assert result["status"] == "fail" and {"reply_lists_lotteries_in_order", "reply_excludes_lotteries"} <= failed


def test_asking_first_then_answering():
    s = SCENARIOS["pilot-004"]
    good = turns("Is the apartment rent-stabilized? Is this a renewal, and when does it start?",
                 "If Good Cause covers it, 10% is above the 8.38% local rent standard "
                 "([RPP § 216](https://www.nysenate.gov/legislation/laws/RPP/216), "
                 "[DHCR notice](https://hcr.ny.gov/good-cause-eviction)).")
    assert grade.grade(s, good)["status"] == "pass"
    bad = turns("Yes, 10% is fine.", "ok")
    assert grade.grade(s, bad)["status"] == "fail"


@pytest.mark.parametrize("value, phrase, ok", [
    (0, "The guideline is 0% for a one-year lease.", True),
    (0, "There is no increase allowed under the guideline.", True),
    (0, "It went up 10% last year.", False),
    (8.38, "above the 8.38% standard", True),
    (4.5, "a 4.5 percent increase", True),
])
def test_percent_detection(value, phrase, ok):
    assert grade._has_percent(phrase, value) is ok


def test_unapproved_links_fail_even_after_the_guard_removed_them():
    s = SCENARIOS["pilot-010"]
    t = turns("Is your apartment rent-stabilized?")
    t[0]["removed_links"] = ["https://nyc-tenant-rights.example.org/renewals"]
    assert any(c["status"] == "fail" for c in grade.grade(s, t)["checks"])


def test_mentions_of_the_limit_go_to_review_not_fail():
    c = {"check": "reply_not_claims_limit", "limit_percent": 8.38}
    status, _ = grade.check(c, ["Good Cause's 8.38% standard does not apply, because the owner lives there."], turns(""))
    assert status == "review"


def _world_env(monkeypatch, tmp_path):
    for key in ("NYCHA_DB", "NYCHA_TODAY", "NYCHA_TRACES_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))


def test_run_one_with_a_scripted_model(monkeypatch, tmp_path):
    _world_env(monkeypatch, tmp_path)
    s = SCENARIOS["pilot-001"]
    answer = text(f"[292 East 51st Street]({D}7650) and [Kingsland Commons]({D}7718), "
                  f"data as of November 10, 2026. [More]({OPEN})")
    factory = lambda: FakeClient(tool_call("search_lotteries", {"borough": "Brooklyn", "household_size": 2,  # noqa: E731
                                                                  "annual_income": 65000}), answer)
    record = runner.run_one(s, str(tmp_path / "traces"), factory)
    assert record["status"] == "pass", record["checks"]
    assert record["turns"][0]["tool_calls"][0]["name"] == "search_lotteries" and record["cost_usd"] > 0


def test_run_one_uses_the_backup_data_for_fallback_scenarios(monkeypatch, tmp_path):
    _world_env(monkeypatch, tmp_path)
    s = next(s for s in SCENARIOS.values() if (s["expected"].get("lottery_query") or {}).get("mode") == "fallback")
    calls = []

    def factory():
        c = FakeClient(tool_call("search_lotteries", {"borough": "Brooklyn", "household_size": 2,
                                                       "annual_income": 65000}), text("done"))
        calls.append(c)
        return c

    record = runner.run_one(s, str(tmp_path / "traces"), factory)
    result = record["turns"][0]["tool_calls"][0]
    assert result["ok"] and record["status"] == "fail"  # "done" is not a passing answer


def test_oversized_message_is_rejected_without_a_model_call(monkeypatch, tmp_path):
    _world_env(monkeypatch, tmp_path)
    s = next(s for s in SCENARIOS.values() if any(c["check"] == "rejected_before_model"
                                                   for c in s["expected"].get("checks", [])))
    record = runner.run_one(s, str(tmp_path / "traces"), lambda: FakeClient())
    assert record["status"] == "pass" and record["cost_usd"] == 0


def test_summary_reports_groups_and_failures():
    records = [{"id": "a", "scenario_group": "coverage", "status": "pass", "checks": [], "cost_usd": 0.1,
                "prompt_version": "p", "model": "m", "tuple": SCENARIOS["pilot-001"]["tuple"], "error": None},
               {"id": "b", "scenario_group": "challenge", "status": "fail", "cost_usd": 0.2, "prompt_version": "p",
                "model": "m", "tuple": SCENARIOS["pilot-002"]["tuple"], "error": None,
                "checks": [{"check": "reply_links", "status": "fail", "detail": "missing links"}]}]
    md = runner.summarize(records)
    assert "| coverage | 1 | 0 | 0 | 0 |" in md and "**b** (challenge): reply_links: missing links" in md
