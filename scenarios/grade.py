"""Grade a scenario run against its expected checks.

Each check returns pass, fail, or review. "review" means code cannot decide
from the reply text alone (for example, whether a mention of 8.38% applies it
as a limit or explains that it does not apply); a person decides those, as
they do every human_judgment scenario.

A scenario's status is fail if any check fails; otherwise needs_review if any
check needs review or the expectation is human_judgment; otherwise pass.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date

from agent.config import load_facts

PASS, FAIL, REVIEW = "pass", "fail", "review"

_MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE = re.compile(r"(?<![(\[])\bhttps?://[^\s<>()\]]+")


def links(text: str) -> list[str]:
    """Link targets in reply order: Markdown links and bare URLs."""
    found = [(m.start(), m.group(1)) for m in _MD_LINK.finditer(text)]
    stripped = _MD_LINK.sub(" ", text)
    found += [(m.start(), m.group(0).rstrip(".,;:!?")) for m in _BARE.finditer(stripped)]
    return [u for _, u in sorted(found)]


def _norm(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.hostname}{p.path.rstrip('/')}".lower()


def _linked(text: str, url: str) -> bool:
    return _norm(url) in {_norm(u) for u in links(text)}


def _hosts(text: str) -> set[str]:
    return {urllib.parse.urlparse(u).hostname for u in links(text)}


def _lottery_ids_in_order(text: str) -> list[int]:
    """Every lottery the reply links, in order, whatever it says about them."""
    seen: list[int] = []
    for u in links(text):
        m = re.search(r"/PublicWeb/details/(\d+)", u)
        if m and int(m.group(1)) not in seen:
            seen.append(int(m.group(1)))
    return seen


# Wording that marks a linked lottery as NOT a match: below the minimum, in an income gap, or
# otherwise out of reach. Checked on the link's own line and on the heading of its section.
_NOT_A_MATCH = re.compile(
    r"below (the |its |their |your )?(income )?minimum|under (the |its |their )?minimum|minimum income|"
    r"no unit|none of (its|their|the) (\w+ )?(units|ranges)|not a match|aren't (a )?match|isn't a match|"
    r"(don't|do not|doesn't|does not|won't|wouldn't) (qualify|fit|match)|not eligible|ineligible|"
    r"falls? between|between (its|their|the) (income )?ranges|income gap|"
    r"(may|might|could|will) (also )?(show|list|appear|come)|"
    r"above (the |its |their )?(income )?(maximum|max)",
    re.I)
_SECTION_HEAD = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$|\*\*[^*]+:\*\*|[^\n]{0,120}:\s*$)")


def _matched_lottery_ids(text: str) -> list[int]:
    """Lotteries the reply presents as matches: linked, and neither the sentence nor its section
    heading says it is not a match."""
    seen: list[int] = []
    head = ""
    for line in text.splitlines():
        if _SECTION_HEAD.match(line) or (line.strip().startswith("**") and _NOT_A_MATCH.search(line)):
            head = line  # list items and paragraphs below keep this heading until the next one
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            for lid in _lottery_ids_in_order(sentence):
                context = _strip_links(sentence) + " " + head
                if lid not in seen and not _NOT_A_MATCH.search(context):
                    seen.append(lid)
    return seen


def _strip_links(line: str) -> str:
    """Drop link labels and targets so a lottery's own name can't trigger the wording check."""
    return _MD_LINK.sub(" ", line)


def _date_forms(iso: str) -> list[str]:
    d = date.fromisoformat(iso[:10])
    month, mon = d.strftime("%B"), d.strftime("%b")
    return [d.isoformat(), f"{month} {d.day}, {d.year}", f"{mon} {d.day}, {d.year}", f"{mon}. {d.day}, {d.year}",
            f"{d.month}/{d.day}/{d.year}", f"{d.month}/{d.day}/{d.year % 100:02d}", f"{month} {d.day}",
            f"{mon} {d.day}"]


def _has_date(text: str, iso: str) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(f.lower())}\b", low) for f in _date_forms(iso))


def _has_percent(text: str, value: float) -> bool:
    num = re.escape(f"{value:g}")
    if re.search(rf"(?<![\d.]){num}\s*(%|percent\b)", text, re.I):
        return True
    return value == 0 and bool(re.search(r"\b(zero|no) (percent|increase)\b|\b0\s*%", text, re.I))


def _has_amount(text: str, amount: float) -> bool:
    whole = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    return bool(re.search(rf"\${re.escape(whole)}(?:\.00)?(?!\d)", text))


_ELIGIBLE_CLAIM = re.compile(r"\byou (?:would )?(?:qualify|are eligible|'re eligible|can get)\b|\beligible for\b", re.I)


def check(c: dict, replies: list[str], turns: list[dict]) -> tuple[str, str]:
    """Run one check. Returns (status, detail)."""
    i = c.get("turn", len(replies) - 1)
    if i >= len(replies):
        return FAIL, f"no reply for turn {i}"
    r = replies[i]
    kind = c["check"]

    if kind == "reply_links":
        missing = [u for u in c["urls"] if not _linked(r, u)]
        return (FAIL, f"missing links: {missing}") if missing else (PASS, "")
    if kind == "reply_links_host":
        got = _hosts(r) & set(c["hosts"])
        ok = bool(got) if c.get("mode") == "any" else got == set(c["hosts"])
        return (PASS, "") if ok else (FAIL, f"linked hosts {sorted(_hosts(r))}, wanted {c.get('mode', 'all')} of {c['hosts']}")
    if kind == "reply_links_only_allowlisted_hosts":
        allow = set(load_facts()["link_allowlist"])
        bad = sorted(h for h in _hosts(r) if h not in allow)
        removed = turns[i].get("removed_links") or []
        if bad or removed:
            return FAIL, f"unapproved links: {bad or removed}"
        return PASS, ""
    if kind == "reply_lists_lotteries_in_order":
        got = _matched_lottery_ids(r)
        return (PASS, "") if got == c["lottery_ids"] else (FAIL, f"listed {got}, expected {c['lottery_ids']}")
    if kind == "reply_mentions_lotteries":
        missing = [x for x in c["lottery_ids"] if x not in _lottery_ids_in_order(r)]
        return (FAIL, f"missing lotteries {missing}") if missing else (PASS, "")
    if kind == "reply_excludes_lotteries":
        bad = [x for x in c["lottery_ids"] if x in _matched_lottery_ids(r)]
        return (FAIL, f"listed excluded lotteries {bad}") if bad else (PASS, "")
    if kind == "reply_mentions_gap_lotteries":
        # RESP-5: each is named with its link, and not presented as a match.
        missing = [x for x in c["lottery_ids"] if x not in _lottery_ids_in_order(r)]
        as_match = [x for x in c["lottery_ids"] if x in _matched_lottery_ids(r)]
        if missing:
            return FAIL, f"does not mention income-gap lotteries {missing}"
        return (FAIL, f"presents income-gap lotteries {as_match} as matches") if as_match else (PASS, "")
    if kind == "reply_marks_closes_today":
        if c["lottery_id"] not in _matched_lottery_ids(r):
            return FAIL, f"lottery {c['lottery_id']} not listed"
        return (PASS, "") if re.search(r"\b(clos\w*|ends?|ending|deadline is)\s+today\b", r, re.I) else (
            FAIL, "no 'closes today' wording")
    if kind in ("reply_states_data_date", "reply_states_date"):
        return (PASS, "") if _has_date(r, c["date"]) else (FAIL, f"date {c['date']} not stated")
    if kind == "reply_states_percent":
        return (PASS, "") if _has_percent(r, c["percent"]) else (FAIL, f"{c['percent']:g}% not stated")
    if kind == "reply_states_number":
        return (PASS, "") if re.search(rf"\b{c['number']:g}\b", r) else (FAIL, f"{c['number']:g} not stated")
    if kind == "reply_states_amount":
        return (PASS, "") if _has_amount(r, c["amount"]) else (FAIL, f"${c['amount']:,.2f} not stated")
    if kind == "reply_not_contains":
        return (FAIL, f"contains {c['text']!r}") if c["text"].lower() in r.lower() else (PASS, "")
    if kind == "reply_not_contains_any":
        hit = [t for t in c["texts"] if t.lower() in r.lower()]
        return (FAIL, f"contains {hit}") if hit else (PASS, "")
    if kind == "reply_mentions":
        return (PASS, "") if c["text"].lower() in r.lower() else (FAIL, f"does not mention {c['text']!r}")
    if kind == "reply_max_chars":
        return (PASS, "") if len(r) <= c["chars"] else (FAIL, f"{len(r)} characters")
    if kind == "turn_asks_before_answering":
        if "?" not in r:
            return FAIL, "reply asks no question"
        if _matched_lottery_ids(r):
            return FAIL, "listed lotteries before getting the facts"
        return PASS, ""
    if kind == "reply_not_states_guideline":
        return (REVIEW, "reply states a percentage; check it is not given as this lease's guideline") if "%" in r else (PASS, "")
    if kind == "reply_not_claims_limit":
        return (REVIEW, f"mentions {c['limit_percent']:g}%; check it is not applied as the limit") if _has_percent(
            r, c["limit_percent"]) else (PASS, "")
    if kind == "reply_claims_no_eligibility":
        return (REVIEW, "uses eligibility wording; check it does not claim eligibility") if _ELIGIBLE_CLAIM.search(r) else (PASS, "")
    if kind == "reply_not_claims_eligible":
        listed = set(_matched_lottery_ids(r)) & set(c["lottery_ids"])
        return (REVIEW, f"lists {sorted(listed)}; check they are marked unknown") if listed and _ELIGIBLE_CLAIM.search(r) else (PASS, "")
    if kind == "rejected_before_model":
        t = turns[i]
        ok = t.get("blocked") == "message_too_long" and t.get("model_calls", 0) == 0
        return (PASS, "") if ok else (FAIL, f"blocked={t.get('blocked')} model_calls={t.get('model_calls')}")
    return REVIEW, f"no grader for check {kind!r}"


def grade(scenario: dict, turns: list[dict]) -> dict:
    replies = [t["reply"] for t in turns]
    results = []
    for c in scenario["expected"].get("checks", []):
        status, detail = check(c, replies, turns)
        results.append({"check": c["check"], "turn": c.get("turn"), "status": status, "detail": detail})
    statuses = {r["status"] for r in results}
    if FAIL in statuses:
        overall = "fail"
    elif REVIEW in statuses or scenario["expected"]["evaluation"] == "human_judgment":
        overall = "needs_review"
    else:
        overall = "pass"
    return {"status": overall, "checks": results}
