"""Check that the spec, sources.yaml, facts.yaml, and the snapshots agree.

    uv run python -m sources.validate
    uv run python -m sources.validate --allow-missing   # before every snapshot exists

Checks:

1. The source ids in SPEC.md GROUND-1 match sources.yaml exactly.
2. Every source id cited in facts.yaml exists in sources.yaml, and every cited
   fact has a `verified` date.
3. Every user-facing source URL and every URL in facts.yaml is on a host in
   facts.yaml `link_allowlist` (SEC-1).
4. Every snapshot source has files in data/sources/<id>/ with complete front
   matter, unique section ids, and a body that matches its sha256.
5. Key numbers in facts.yaml appear in the saved source text, so a fact
   cannot drift from its source unnoticed.

Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.parse
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONT_MATTER_FIELDS = {"source_id", "section_id", "title", "url", "order", "retrieved", "sha256"}


def load_yaml(name: str):
    return yaml.safe_load((REPO_ROOT / name).read_text())


def spec_source_ids() -> set[str]:
    """Source ids from the GROUND-1 table: rows that start with | `id` |."""
    spec = (REPO_ROOT / "SPEC.md").read_text()
    table = spec.split("**GROUND-1.**", 1)[1].split("**GROUND-2.**", 1)[0]
    return set(re.findall(r"^\| `([a-z0-9-]+)` \|", table, re.M))


def cited_sources(node, path=""):
    """Yield (path, block) for every source-citing block in facts.yaml."""
    if isinstance(node, dict):
        if "id" in node and "url" in node:
            yield path, node
        for key, value in node.items():
            yield from cited_sources(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from cited_sources(value, f"{path}[{i}]")


def urls_in(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from urls_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from urls_in(value)
    elif isinstance(node, str) and node.startswith("http"):
        yield node


def read_section(path: Path) -> tuple[dict, str]:
    _, front, body = path.read_text().split("---\n", 2)
    return yaml.safe_load(front), body


def section_text(source_id: str) -> str:
    folder = REPO_ROOT / "data" / "sources" / source_id
    return "\n".join(read_section(p)[1] for p in sorted(folder.glob("*.md")))


def pct(value: float) -> str:
    """Regex for a percentage as the sources print it: 3, 3.0, 4.5, 3.38."""
    # The lookbehind stops 0% from matching inside 10%.
    return r"(?<![\d.])" + re.escape(f"{value:g}") + r"\s*%"


def usd(value: int) -> str:
    return re.escape(f"${value:,}")


def fact_checks(facts: dict) -> list[tuple[str, str, str]]:
    """(description, source_id, regex) for each number checked against its source."""
    checks = []
    for order in facts["rgb_apartment_orders"]:
        for term in ("one_year", "two_year"):
            value = order[f"{term}_lease_increase_percent"]
            checks.append(
                (f"Order #{order['order']} {term} lease {value}%", "rgb-apartment-orders",
                 rf"{term.replace('_', '-')} lease commencing on or after {order['lease_start_from']:%B} [^%]*?{pct(value)}")
            )
    for year in facts["good_cause"]["local_rent_standard"]["published"]:
        # The notice's CPI table has two columns, NYC region first, then the
        # Northeast region, so the NYC figure is the first one after the label.
        checks.append((f"CPI change {year['cpi_change_percent']}% (NYC region)", "dhcr-good-cause",
                       rf"Annual % Change in\s+Consumer Price Index for\s+{pct(year['cpi_change_percent'])}"
                       rf".*?\({re.escape(year['cpi_period'])}\)"))
    lux = facts["good_cause"]["exemptions"]["luxury_threshold_usd_by_unit_size"]
    row = r"\s+".join(usd(lux[k]) for k in ("studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom"))
    checks.append(("245% FMR row for NYC counties", "dhcr-good-cause", r"Kings County\s+" + row))
    deposit = facts["security_deposit"]
    if deposit["max_months_rent"] == 1:
        checks.append(("deposit cap one month (FAQ)", "rgb-faq-security-deposits", r"no more than one month.s rent"))
        checks.append(("deposit cap one month (statute)", "hstpa-2019", r"shall exceed the amount of one month.s rent"))
    checks.append((f"deposit return {deposit['return_deadline_days']} days", "rgb-faq-security-deposits",
                   r"within (fourteen|14) days"))
    fees = facts["fees"]
    if fees["application_fee_max_usd"] == 20:
        checks.append(("application fee $20", "nys-rpp-a7", r"or\s+twenty\s+dollars,\s+whichever\s+is\s+less"))
    if (fees["late_fee_max_usd"], fees["late_fee_max_percent_of_rent"]) == (50, 5):
        checks.append(("late fee $50 / 5%", "nys-rpp-a7", r"fifty\s+dollars\s+or\s+five\s+percent"))
    lrs = facts["good_cause"]["local_rent_standard"]
    if (lrs["inflation_index_base_percent"], lrs["cap_percent"]) == (5, 10):
        checks.append(("inflation index base 5%", "nys-rpp-a6a", r"five\s+percent\s+plus\s+the\s+annual\s+percentage\s+change"))
        checks.append(("local rent standard cap 10%", "nys-rpp-a6a", r"inflation\s+index\s+or\s+ten\s+percent,\s+whichever\s+is\s+lower"))
    assets = facts["lottery_assets"]
    for ami, limit in assets["rental_asset_limit_usd_by_ami_percent"].items():
        checks.append((f"rental asset limit at {ami}% AMI", "hdc-asset-limits",
                       rf"(?m)^{ami}%\s+{usd(limit)}\b"))
    checks.append((f"no property within {assets['rental_no_residential_property_within_miles']} miles",
                   "hdc-asset-limits", r"within\s+one\s+hundred\s+\(100\)\s+miles"))
    checks.append(("retirement and college savings excluded", "hdc-asset-limits",
                   r"retirement\s+funds\s+and\s+college\s+savings\s+accounts\s+are\s+not\s+subject"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-missing", action="store_true",
                        help="warn instead of fail when a snapshot source has no files yet")
    args = parser.parse_args(argv)

    errors: list[str] = []
    warnings: list[str] = []
    registry = {s["id"]: s for s in load_yaml("sources.yaml")["sources"]}
    facts = load_yaml("facts.yaml")
    allow = set(facts["link_allowlist"])

    # 1. Spec and registry agree.
    spec_ids = spec_source_ids()
    for sid in sorted(spec_ids - registry.keys()):
        errors.append(f"SPEC.md lists {sid!r} but sources.yaml does not")
    for sid in sorted(registry.keys() - spec_ids):
        errors.append(f"sources.yaml lists {sid!r} but SPEC.md GROUND-1 does not")

    # 2. Facts cite known sources and are verified.
    for path, block in cited_sources(facts):
        if block["id"] not in registry:
            errors.append(f"facts.yaml {path} cites unknown source {block['id']!r}")
        if not block.get("verified"):
            errors.append(f"facts.yaml {path} has no verified date")

    # 3. Links stay on allowlisted hosts.
    user_urls = [s["url"] for s in registry.values() if s.get("user_facing", True)]
    fact_urls = [u for u in urls_in(facts) if u != facts["lottery_sync"]["api_base_url"]]
    for url in user_urls + fact_urls:
        host = urllib.parse.urlparse(url).hostname
        if host not in allow:
            errors.append(f"{url} is on {host}, which is not in link_allowlist")

    # 4. Snapshots are complete and intact.
    missing: set[str] = set()
    for sid, source in registry.items():
        if source["kind"] != "snapshot":
            continue
        files = sorted((REPO_ROOT / "data" / "sources" / sid).glob("*.md"))
        if not files:
            missing.add(sid)
            (warnings if args.allow_missing else errors).append(
                f"no snapshot for {sid!r}; run: uv run python -m sources.snapshot --only {sid}")
            continue
        seen: set[str] = set()
        for f in files:
            front, body = read_section(f)
            if lacking := FRONT_MATTER_FIELDS - front.keys():
                errors.append(f"{f.name}: missing front matter {sorted(lacking)}")
                continue
            if front["source_id"] != sid:
                errors.append(f"{f.name}: source_id {front['source_id']!r} is in folder {sid!r}")
            if front["section_id"] in seen:
                errors.append(f"{f.name}: duplicate section_id {front['section_id']!r}")
            seen.add(front["section_id"])
            text = body.split("\n", 3)[3].rstrip("\n") if body.count("\n") >= 3 else ""
            if hashlib.sha256(text.encode()).hexdigest() != front["sha256"]:
                errors.append(f"{f.name}: body does not match sha256 (edited by hand?)")

    # 5. Numbers in facts.yaml appear in their sources.
    checked = 0
    for desc, sid, pattern in fact_checks(facts):
        if sid in missing:
            warnings.append(f"not checked, no snapshot for {sid}: {desc}")
            continue
        if re.search(pattern, section_text(sid), re.I | re.S):
            checked += 1
        else:
            errors.append(f"fact not found in {sid} snapshot: {desc}")

    for w in warnings:
        print(f"warning  {w}")
    for e in errors:
        print(f"ERROR    {e}")
    print(f"{len(errors)} errors, {len(warnings)} warnings, {checked} facts matched their sources")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
