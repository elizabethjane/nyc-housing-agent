"""Accept critic rewrites only when they keep every fact.

    uv run python -m scenarios.apply_reviews <review.jsonl> [...]

A critic may reword a user message but must not change a fact the expected
outcome depends on. A rewrite is accepted only if its numbers (with $ and %),
month names, and NYC places are exactly those of the original, and its
number of followups is unchanged. Accepted rewrites go to
scenarios/message_revisions.json, which build_full.py applies; rejected ones
are listed with the reason.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from scenarios.validate import load_jsonl

REVISIONS = Path(__file__).resolve().parent / "message_revisions.json"

MONTHS = r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?"
# Places that appear in the scenarios. Abbreviations count as the borough.
PLACES = {
    "bronx": "bronx", "brooklyn": "brooklyn", "bk": "brooklyn", "brookyln": "brooklyn", "queens": "queens",
    "manhattan": "manhattan", "staten island": "staten island", "yonkers": "yonkers", "hoboken": "hoboken",
    "jersey city": "jersey city", "hempstead": "hempstead", "long island": "long island",
    "white plains": "white plains", "westchester": "westchester",
}
NEIGHBORHOODS = [
    "fordham", "astoria", "bushwick", "flushing", "ridgewood", "bay ridge", "long island city", "downtown brooklyn",
    "bed-stuy", "bed stuy", "bedford-stuyvesant", "corona", "kensington", "east harlem", "harlem", "washington heights",
    "park slope", "greenpoint", "canarsie", "woodside", "chelsea", "mott haven", "elmhurst", "inwood", "sunnyside",
    "jamaica", "riverdale", "flatbush", "jackson heights", "upper west side", "sunset park", "south bronx",
    "crown heights", "kew gardens hills", "kew gardens", "ozone park", "ozone prk", "woodhaven", "west village",
    "prospect heights", "williamsburg", "kingsbridge", "maspeth", "midtown", "tribeca", "bensonhurst",
]


def facts(text: str) -> Counter:
    t = text.lower()
    found: Counter = Counter()
    for m in re.finditer(r"\$?\d[\d,]*(\.\d+)?\s*(k\b|%|percent)?", t):
        token = re.sub(r"\s+", "", m.group(0)).replace(",", "").replace("percent", "%")
        found[f"num:{token}"] += 1
    for m in re.finditer(rf"\b({MONTHS})\b", t):
        found[f"month:{m.group(1)[:3]}"] += 1
    for name, canonical in PLACES.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            found[f"place:{canonical}"] = 1
    for name in NEIGHBORHOODS:
        if re.search(rf"\b{re.escape(name)}\b", t):
            found[f"place:{name.replace('-', ' ')}"] = 1
    return found


def check(original: dict, review: dict) -> str | None:
    """None if the rewrite keeps every fact, else the reason it does not."""
    if len(review["followups"]) != len(original["followups"]):
        return "changed the number of followups"
    before = facts(" ".join([original["opening_message"], *original["followups"]]))
    after = facts(" ".join([review["opening_message"], *review["followups"]]))
    if before != after:
        lost = sorted((before - after).elements())
        added = sorted((after - before).elements())
        return f"facts changed: lost {lost}, added {added}"
    return None


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    originals = {r["id"]: r for r in load_jsonl(root / "scenarios" / "full_scenarios.jsonl")}
    revisions = json.loads(REVISIONS.read_text()) if REVISIONS.exists() else {}
    accepted = rejected = kept = 0
    for path in argv:
        for review in load_jsonl(Path(path)):
            if review["verdict"] != "rewrite":
                kept += 1
                continue
            orig = originals[review["id"]]
            if orig["tuple"]["intention"] == "spend_abuse":
                print(f"REJECT   {review['id']}: spend_abuse messages are never rewritten")
                rejected += 1
                continue
            if reason := check(orig, review):
                print(f"REJECT   {review['id']}: {reason}")
                rejected += 1
                continue
            revisions[review["id"]] = {
                "opening_message": review["opening_message"],
                "followups": review["followups"],
                "issues": review["issues"],
                "original_opening": orig["opening_message"],
            }
            accepted += 1
    REVISIONS.write_text(json.dumps(dict(sorted(revisions.items())), indent=1) + "\n")
    print(f"{kept} kept, {accepted} rewrites accepted, {rejected} rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
