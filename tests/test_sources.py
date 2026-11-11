"""Offline checks on sources.yaml, facts.yaml, and the saved snapshots."""

import copy
import re

import pytest

from sources import validate
from sources.snapshot import html_sections


@pytest.fixture(scope="module")
def facts():
    return validate.load_yaml("facts.yaml")


def test_repository_validates():
    assert validate.main(["--allow-missing"]) == 0


def test_spec_and_registry_list_the_same_sources():
    registry = {s["id"] for s in validate.load_yaml("sources.yaml")["sources"]}
    assert validate.spec_source_ids() == registry


def _failing_checks(facts):
    out = []
    for desc, sid, pattern in validate.fact_checks(facts):
        files = list((validate.REPO_ROOT / "data" / "sources" / sid).glob("*.md"))
        if files and not re.search(pattern, validate.section_text(sid), re.I | re.S):
            out.append(desc)
    return out


@pytest.mark.parametrize(
    "path, wrong",
    [
        (("rgb_apartment_orders", 0, "one_year_lease_increase_percent"), 10),
        (("rgb_apartment_orders", 1, "two_year_lease_increase_percent"), 3),
        (("good_cause", "local_rent_standard", "published", 0, "cpi_change_percent"), 3.15),
        (("good_cause", "exemptions", "luxury_threshold_usd_by_unit_size", "studio"), 6000),
    ],
)
def test_a_wrong_number_is_caught(facts, path, wrong):
    bad = copy.deepcopy(facts)
    node = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = wrong
    assert _failing_checks(bad), f"changing {path} to {wrong} was not detected"


def test_zero_percent_does_not_match_ten_percent():
    assert not re.search(validate.pct(0), "a one-year lease: 10%")
    assert re.search(validate.pct(0), "a one-year lease: 0%")


def test_html_sections_split_at_headings_with_anchors():
    html = (
        '<p>Intro <a href="/x">link</a></p>'
        '<h5 id="right">Can they?</h5><p>Yes.</p>'
        '<p><a href="#top">Back to Top</a></p>'
        "<h2>Empty</h2>"
    )
    secs = html_sections(html, "https://example.org/faq/", "FAQ", "faq")
    assert [s.section_id for s in secs] == ["faq", "faq--right"]
    assert secs[0].text == "Intro [link](https://example.org/x)"
    assert secs[1].url == "https://example.org/faq/#right"
    assert secs[1].text == "Yes."
