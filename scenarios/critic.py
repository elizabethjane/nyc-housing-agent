"""Automated critic pass over a scenario file, plus a coverage report.

    uv run python -m scenarios.critic scenarios/full_scenarios.jsonl

Flags user messages that contain things a renter would not know or say
(internal ids, statute and order numbers, computed figures, spec terms), and
opening messages that share a template. Then counts each dimension value per
group so gaps are visible. Exits 1 if any message is flagged.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from scenarios.validate import DIMENSIONS, load_jsonl

# Phrases a renter would not type: answers, internal names, and spec terms.
LEAKS = [
    (r"\border\s*#?\s*5[78]\b", "Rent Guidelines Board order number"),
    (r"8\.38", "computed local rent standard"),
    (r"local rent standard|inflation index", "Good Cause term of art"),
    (r"\bRPP\b|§|section\s+2\d\d", "statute citation"),
    (r"\b7[0-9]{3}\b", "lottery id"),
    (r"\b(SCOPE|RESP|GROUND|OPEN|SEC|LIMIT|REF)-\d", "spec requirement id"),
    (r"search_lotteries|get_lottery|search_sources|tool call", "tool name"),
    (r"245%|fair market rent", "luxury exemption term of art"),
]
# Malicious scenarios quote tool-ish or system text on purpose.
LEAK_EXEMPT_INTENTIONS = {"prompt_extraction", "instruction_override", "spend_abuse"}


def critique(rows: list[dict]) -> list[str]:
    flags = []
    openings: dict[str, str] = {}
    for row in rows:
        texts = [row["opening_message"], *row["followups"]]
        if row["tuple"]["intention"] not in LEAK_EXEMPT_INTENTIONS:
            for text in texts:
                for pattern, what in LEAKS:
                    if re.search(pattern, text, re.I):
                        flags.append(f"{row['id']}: message contains a {what}: {text[:80]!r}")
        # Same first five words as another scenario suggests a shared template.
        head = " ".join(re.findall(r"\w+", row["opening_message"].lower())[:5])
        if head in openings and len(head.split()) == 5:
            flags.append(f"{row['id']}: opening starts like {openings[head]}: {head!r}")
        openings.setdefault(head, row["id"])
    return flags


def coverage(rows: list[dict]) -> str:
    lines = []
    for dim, values in DIMENSIONS.items():
        lines.append(f"{dim}:")
        for group in ("coverage", "challenge"):
            counts = Counter(r["tuple"][dim] for r in rows if r["scenario_group"] == group)
            missing = sorted(values - counts.keys())
            shown = ", ".join(f"{v} {n}" for v, n in counts.most_common())
            lines.append(f"  {group:<9} {shown}" + (f"  | none: {', '.join(missing)}" if missing else ""))
    turns = Counter(r["tuple"]["turn_count"] for r in rows)
    lines.append("turn_count: " + ", ".join(f"{k} turns {v}" for k, v in sorted(turns.items())))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    rows = load_jsonl(Path(argv[0]))
    flags = critique(rows)
    for f in flags:
        print(f"FLAG     {f}")
    print(coverage(rows))
    print(f"{len(flags)} flagged")
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
