"""Validate a scenario file before any agent run.

    uv run python -m scenarios.validate scenarios/pilot_scenarios.jsonl

Checks the format and dimension values, and that each expected outcome is
grounded:

- Lottery scenarios: the expected lottery ids, in order, equal what
  scenarios.lottery_answers computes from the cited fixture.
- facts.yaml scenarios: every cited path resolves, and every percentage a
  check expects appears in the cited facts.
- Every URL a check expects to be linked is on an allowlisted host.

Exits 1 on any error.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

import yaml

from scenarios import lottery_answers

REPO_ROOT = Path(__file__).resolve().parents[1]

# The approved dimensions (scenarios/README.md).
DIMENSIONS = {
    "topic": {"rent_increase", "lease_renewal", "eviction", "security_deposit", "fees",
              "lottery_search", "lottery_apply", "lottery_assets", "none"},
    "style": {"precise_formal", "casual", "vague", "typo_heavy", "rambling_irrelevant",
              "terse", "frustrated"},
    "scope": {"supported", "nyc_housing_unsupported", "unrelated"},
    "intention": {"neutral", "instruction_override", "prompt_extraction", "link_injection",
                  "legal_advice_roleplay", "spend_abuse"},
    "information_given": {"complete", "missing", "user_unsure", "corrected_later"},
    "apartment_type": {"rent_stabilized", "rent_controlled", "good_cause_covered",
                       "good_cause_exempt", "unknown", "not_applicable"},
    "location": {"borough_named", "outside_nyc", "not_stated"},
    "lottery_situation": {"several_matches", "no_matches", "closes_today", "closed_named",
                          "mitchell_lama", "eligibility_unknown", "fallback_mode",
                          "not_applicable"},
    "edge_position": {"none", "order_boundary", "no_order_for_date", "income_at_min",
                      "income_at_max", "below_min_with_subsidy", "rent_at_luxury_limit"},
}
GROUPS = {"coverage", "challenge"}
SOURCE_TYPES = {"lottery_fixture", "facts_yaml", "source_snapshot", "specification"}
LOTTERY_TOPICS = {"lottery_search", "lottery_apply"}
TOP_FIELDS = {"id", "scenario_group", "as_of", "tuple", "opening_message", "followups", "expected"}


def resolve(facts: dict, path: str):
    """Resolve 'a.b[0].c' or 'a[order=58].c' against facts.yaml."""
    node = facts
    for part in re.findall(r"[^.\[\]]+|\[[^\]]+\]", path):
        if part.startswith("["):
            key = part[1:-1]
            if "=" in key:
                field, value = key.split("=", 1)
                node = next(x for x in node if str(x.get(field)) == value)
            else:
                node = node[int(key)]
        else:
            node = node[part]
    return node


def numbers_in(node) -> set[float]:
    if isinstance(node, bool):
        return set()
    if isinstance(node, (int, float)):
        return {float(node)}
    if isinstance(node, dict):
        return set().union(*(numbers_in(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(numbers_in(v) for v in node)) if node else set()
    return set()


def validate(rows: list[dict]) -> list[str]:
    errors: list[str] = []
    facts = yaml.safe_load((REPO_ROOT / "facts.yaml").read_text())
    allow = set(facts["link_allowlist"])
    seen_ids: set[str] = set()
    seen_openings: dict[str, str] = {}

    for n, row in enumerate(rows, start=1):
        label = row.get("id", f"line {n}")
        if missing := TOP_FIELDS - row.keys():
            errors.append(f"{label}: missing fields {sorted(missing)}")
            continue
        if label in seen_ids:
            errors.append(f"{label}: duplicate id")
        seen_ids.add(label)
        if row["scenario_group"] not in GROUPS:
            errors.append(f"{label}: scenario_group must be one of {sorted(GROUPS)}")

        t = row["tuple"]
        for dim, values in DIMENSIONS.items():
            if t.get(dim) not in values:
                errors.append(f"{label}: tuple.{dim}={t.get(dim)!r} is not one of {sorted(values)}")
        if t.get("turn_count") != 1 + len(row["followups"]):
            errors.append(f"{label}: turn_count must be 1 + number of followups")
        # Supported lottery questions name a lottery situation; nothing else does.
        needs_situation = t.get("topic") in LOTTERY_TOPICS and t.get("scope") == "supported"
        if (t.get("lottery_situation") != "not_applicable") != needs_situation:
            errors.append(f"{label}: lottery_situation must be set exactly for supported lottery questions")

        opening = re.sub(r"\W+", " ", row["opening_message"].lower()).strip()
        if opening in seen_openings:
            errors.append(f"{label}: same opening message as {seen_openings[opening]}")
        seen_openings[opening] = label

        exp = row["expected"]
        if exp.get("evaluation") not in {"objective", "human_judgment"}:
            errors.append(f"{label}: expected.evaluation must be objective or human_judgment")
        if exp.get("evaluation") == "objective" and not exp.get("outcome"):
            errors.append(f"{label}: objective expectation needs an outcome")
        if exp.get("evaluation") == "human_judgment" and not exp.get("criterion"):
            errors.append(f"{label}: human_judgment expectation needs a criterion")
        source = exp.get("source") or {}
        if source.get("type") not in SOURCE_TYPES:
            errors.append(f"{label}: source.type must be one of {sorted(SOURCE_TYPES)}")
        checks = exp.get("checks", [])

        # Grounding: lottery answers recomputed from the fixture.
        if (query := exp.get("lottery_query")) and query.get("mode") == "fallback":
            ids, _ = lottery_answers.fallback_open(row["as_of"], query["borough"])
            if ids != exp.get("lottery_ids_any_order"):
                errors.append(f"{label}: fallback fixture gives {ids}, scenario expects {exp.get('lottery_ids_any_order')}")
        elif query:
            try:
                fixture = lottery_answers.load_fixture(row["as_of"])
            except FileNotFoundError:
                errors.append(f"{label}: no lottery fixture for as_of {row['as_of']}")
            else:
                found = lottery_answers.matches(
                    fixture, query["borough"], query["household_size"], query["income"],
                    frozenset(query.get("unknown_ids", [])))
                got = [m["lottery_id"] for m in found]
                if got != exp.get("lottery_ids_in_order"):
                    errors.append(f"{label}: fixture gives {got}, scenario expects {exp.get('lottery_ids_in_order')}")
                for c in checks:
                    if c["check"] == "reply_lists_lotteries_in_order" and c["lottery_ids"] != got:
                        errors.append(f"{label}: check lists {c['lottery_ids']}, fixture gives {got}")
                    if c["check"] == "reply_excludes_lotteries" and set(c["lottery_ids"]) & set(got):
                        errors.append(f"{label}: excludes eligible lotteries {sorted(set(c['lottery_ids']) & set(got))}")
                # The labeled lottery situation must match the data.
                situation = t.get("lottery_situation")
                if situation == "no_matches" and got:
                    errors.append(f"{label}: labeled no_matches but the fixture gives {got}")
                if situation in {"several_matches", "closes_today", "eligibility_unknown"} and not got:
                    errors.append(f"{label}: labeled {situation} but the fixture gives no matches")
                if situation == "closes_today" and not any(m["closes_today"] for m in found):
                    errors.append(f"{label}: labeled closes_today but no match ends on {row['as_of']}")
                if situation == "eligibility_unknown" and not any(m["eligibility"] == "unknown" for m in found):
                    errors.append(f"{label}: labeled eligibility_unknown but no match has unknown eligibility")

        # Grounding: a cited saved-source section exists.
        if source.get("type") == "source_snapshot":
            sid, _, section = source["reference"].partition("/")
            folder = REPO_ROOT / "data" / "sources" / sid
            if not any(p.stem.split("-", 1)[1] == section for p in folder.glob("*.md")):
                errors.append(f"{label}: no saved section {source['reference']}")

        # Grounding: cited facts resolve and contain the expected numbers.
        if source.get("type") == "facts_yaml":
            cited: set[float] = set()
            for path in (p.strip() for p in source["reference"].split(";")):
                try:
                    cited |= numbers_in(resolve(facts, path))
                except (KeyError, IndexError, StopIteration, TypeError):
                    errors.append(f"{label}: facts.yaml has no {path!r}")
            for c in checks:
                for key in ("percent", "number"):
                    if key in c and float(c[key]) not in cited:
                        errors.append(f"{label}: expects {key} {c[key]} but the cited facts do not contain it")

        # Expected links stay on allowlisted hosts.
        for c in checks:
            for url in c.get("urls", []):
                host = urllib.parse.urlparse(url).hostname
                if host not in allow:
                    errors.append(f"{label}: expected link {url} is not on an allowlisted host")
            for host in c.get("hosts", []):
                if host not in allow:
                    errors.append(f"{label}: expected host {host} is not allowlisted")

    return errors


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    rows = load_jsonl(Path(argv[0]))
    errors = validate(rows)
    for e in errors:
        print(f"ERROR    {e}")
    groups = {g: sum(r.get("scenario_group") == g for r in rows) for g in sorted(GROUPS)}
    print(f"{len(rows)} scenarios ({', '.join(f'{v} {k}' for k, v in groups.items())}), {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
