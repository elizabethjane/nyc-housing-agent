"""The agent's tools (SPEC.md section 5). All read-only.

Each tool is a plain function returning a dict: `ok: true` plus fields, or
`ok: false` with an `error` code and a `reason`. TOOL_SPECS are the
definitions sent to Claude; run_tool() dispatches a call by name and turns
bad arguments into `invalid_argument` results instead of exceptions.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Callable

from agent import db, eligibility, rent_rules
from agent.config import load_facts, load_registry, today
from agent.killswitch import paused_reason
from agent.sources import get_index

SNIPPET_CHARS = 400
SECTION_CHARS = 12000


def _err(code: str, reason: str) -> dict:
    return {"ok": False, "error": code, "reason": reason}


# --- TOOL-1, TOOL-2: saved law sources ---------------------------------------


def search_sources(query: str, source_id: str | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        return _err("invalid_argument", "empty query")
    index = get_index()
    if source_id and source_id not in {s.source_id for s in index.sections}:
        return _err("not_found", f"{source_id!r} is not a saved source")
    hits = index.search(query, source_id=source_id, k=6)
    return {"ok": True, "results": [
        {"source_id": s.source_id, "section_id": s.section_id, "title": s.title, "url": s.url,
         "snippet": s.text[:SNIPPET_CHARS], "score": round(score, 2)} for s, score in hits]}


def get_source_section(source_id: str, section_id: str) -> dict:
    s = get_index().by_key.get((source_id, section_id))
    if s is None:
        return _err("not_found", f"no section {section_id!r} in {source_id!r}; use search_sources to find one")
    return {"ok": True, "source_id": s.source_id, "section": s.section_id, "title": s.title, "url": s.url,
            "retrieved": s.retrieved, "text": s.text[:SECTION_CHARS],
            "truncated": len(s.text) > SECTION_CHARS}


# --- TOOL-3, TOOL-4: lotteries -------------------------------------------------


def _data_status(conn: sqlite3.Connection) -> dict:
    mode = db.get_meta(conn, "source_mode") or "api"
    status = {"data_as_of": db.get_meta(conn, "data_as_of"), "source_mode": mode,
              "today": today().isoformat(),
              "open_lotteries_url": load_facts()["lottery_sync"]["open_lotteries_url"]}
    if mode == "fallback":
        status["note"] = ("Backup data from NYC Open Data, refreshed monthly. It may be up to a month old and has "
                          "no income limits, so eligibility is unknown for every lottery. Newer lotteries may be "
                          "missing.")
    return status


def _summary(lot: dict, on: date) -> dict:
    end = date.fromisoformat(lot["end_date"])
    return {"lottery_id": lot["lottery_id"], "name": lot["name"], "boroughs": lot["boroughs"],
            "neighborhood": lot["neighborhood"], "addresses": lot["addresses"], "end_date": lot["end_date"],
            "days_left": (end - on).days, "closes_today": end == on,
            "is_waiting_list": lot["is_mitchell_lama"], "url": eligibility.property_url(lot["lottery_id"])}


def search_lotteries(borough: str, household_size: int, annual_income: float) -> dict:
    if reason := paused_reason("search_lotteries"):
        return _err("paused", reason)
    b = eligibility.normalize_borough(borough or "")
    if b is None:
        return _err("invalid_argument", f"unknown borough {borough!r}; use Bronx, Brooklyn, Manhattan, Queens, "
                                        f"or Staten Island")
    if household_size < 1 or household_size > 20:
        return _err("invalid_argument", "household_size must be between 1 and 20")
    if annual_income < 0:
        return _err("invalid_argument", "annual_income cannot be negative")
    on = today()
    with db.connection() as conn:
        if db.get_meta(conn, "data_as_of") is None:
            return _err("unavailable", "lottery data has not been synced yet")
        listed, below, not_eligible = [], [], 0
        for lot in eligibility.open_lotteries(conn, on, b):
            result = eligibility.assess(conn, lot, household_size, annual_income)
            if result["eligibility"] in ("eligible", "unknown"):
                listed.append({**_summary(lot, on), **result})
            elif result["eligibility"] == "below_minimum":
                below.append({**_summary(lot, on), **result})
            else:
                not_eligible += 1
        return {"ok": True, **_data_status(conn), "borough": b, "household_size": household_size,
                "annual_income": annual_income, "count": len(listed), "lotteries": listed,
                "below_minimum": below, "open_but_not_eligible": not_eligible}


def get_lottery(lottery_id: int) -> dict:
    if reason := paused_reason("get_lottery"):
        return _err("paused", reason)
    on = today()
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM lotteries WHERE lottery_id = ?", (lottery_id,)).fetchone()
        if row is None:
            return _err("not_found", f"lottery {lottery_id} is not among the synced lotteries, so it is not "
                                     f"currently open")
        lot = db.lottery_row(row)
        return {"ok": True, **_data_status(conn), **_summary(lot, on),
                "open_for_applications": eligibility.is_open(lot, on), "start_date": lot["start_date"],
                "set_asides": lot["set_asides"], "has_income_table": lot["has_income_table"],
                "income_table": eligibility.income_rows(conn, lottery_id)}


def find_lottery(name: str) -> dict:
    """Look up synced lotteries by (part of) their name, for questions about a named lottery."""
    words = [w for w in (name or "").lower().replace("&", " ").split() if len(w) > 1]
    if not words:
        return _err("invalid_argument", "empty name")
    on = today()
    with db.connection() as conn:
        rows = [db.lottery_row(r) for r in conn.execute("SELECT * FROM lotteries")]
        hits = [r for r in rows if all(w in r["name"].lower().replace("&", " ") for w in words)]
        return {"ok": True, **_data_status(conn), "matches": [
            {**_summary(r, on), "open_for_applications": eligibility.is_open(r, on)} for r in hits[:5]],
            "note": "A lottery missing here is not currently open (or not synced)." if not hits else None}


# --- TOOL-5: referrals ---------------------------------------------------------

REFERRALS = {
    "legal_help": ["nyc-tenant-protection"],
    "lottery_application_status": ["housing-connect"],
    "deposit_or_fee_complaint": ["ag-rent-security-complaint", "rgb-faq-security-deposits"],
    "open_lotteries": ["housing-connect"],
    "rent_stabilized_increases": ["rgb-apartment-orders"],
    "good_cause": ["nys-rpp-a6a", "dhcr-good-cause"],
}


def refer_to_resource(topic: str) -> dict:
    ids = REFERRALS.get(topic)
    if ids is None:
        return _err("not_found", f"no approved resource for {topic!r}; topics: {sorted(REFERRALS)}")
    reg = load_registry()
    resources = [{"name": reg[i]["title"], "url": reg[i]["url"]} for i in ids]
    if topic == "open_lotteries":
        resources[0]["url"] = load_facts()["lottery_sync"]["open_lotteries_url"]
    if topic == "legal_help":
        resources.append({"name": "Tenant Helpline", "how": "call 311 and ask for the Tenant Helpline"})
    return {"ok": True, "topic": topic, "resources": resources}


# --- TOOL-6: rent rules ----------------------------------------------------------


def check_rent_rules(apartment_type: str, lease_start_date: str | None = None,
                     term_years: int | None = None) -> dict:
    if apartment_type == "rent_stabilized":
        missing = [n for n, v in (("lease_start_date", lease_start_date), ("term_years", term_years)) if not v]
        if missing:
            return _err("missing_input", f"ask the user for: {', '.join(missing)} (the start date of the NEW lease, "
                                         f"and whether it is a 1- or 2-year lease)")
        try:
            start = date.fromisoformat(lease_start_date)
        except ValueError:
            return _err("invalid_argument", "lease_start_date must be YYYY-MM-DD")
        return rent_rules.stabilized_guideline(start, int(term_years))
    if apartment_type == "not_regulated":
        return rent_rules.good_cause_standard()
    if apartment_type == "rent_controlled":
        return _err("not_covered", "the approved sources do not give rent-controlled increase limits; neither the "
                                   "Rent Guidelines Board orders nor Good Cause apply")
    return _err("invalid_argument", "apartment_type must be rent_stabilized, not_regulated, or rent_controlled")


# --- Definitions sent to Claude and dispatch ----------------------------------------

SOURCE_IDS = sorted(sid for sid, s in load_registry().items() if s["kind"] == "snapshot")

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "search_sources",
     "description": ("Search the saved text of the approved NYC and NY State tenant-law sources. Returns matching "
                     "sections with their links. Use it for any question about tenant law before answering."),
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "What to look for, in plain words"},
         "source_id": {"type": "string", "enum": SOURCE_IDS, "description": "Optional: limit to one source"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "get_source_section",
     "description": "Read the full text of one saved section found with search_sources.",
     "input_schema": {"type": "object", "properties": {
         "source_id": {"type": "string", "enum": SOURCE_IDS}, "section_id": {"type": "string"}},
         "required": ["source_id", "section_id"], "additionalProperties": False}},
    {"name": "search_lotteries",
     "description": ("List open NYC rental housing lotteries in one borough and the household's eligibility, "
                     "computed from each lottery's income table. Only call it once you know the borough, the "
                     "number of people in the household, and the annual household income."),
     "input_schema": {"type": "object", "properties": {
         "borough": {"type": "string", "enum": list(eligibility.BOROUGHS)},
         "household_size": {"type": "integer", "minimum": 1},
         "annual_income": {"type": "number", "minimum": 0, "description": "Total yearly household income in USD"}},
         "required": ["borough", "household_size", "annual_income"], "additionalProperties": False}},
    {"name": "get_lottery",
     "description": "Details of one synced lottery by id: dates, open status, set-asides, and its income table.",
     "input_schema": {"type": "object", "properties": {"lottery_id": {"type": "integer"}},
                      "required": ["lottery_id"], "additionalProperties": False}},
    {"name": "find_lottery",
     "description": ("Find a lottery by name or street address when the user names one. A lottery that is not "
                     "found is not currently open."),
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"], "additionalProperties": False}},
    {"name": "refer_to_resource",
     "description": "Approved resources to point the user to for a topic, with links.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string", "enum": sorted(REFERRALS)}},
                      "required": ["topic"], "additionalProperties": False}},
    {"name": "check_rent_rules",
     "description": ("Rent increase limits from the official figures. rent_stabilized: the Rent Guidelines Board "
                     "guideline for a renewal, which needs the NEW lease's start date and term. not_regulated: "
                     "the Good Cause local rent standard and the exemptions to ask about. Use this for every rent "
                     "increase number you state."),
     "input_schema": {"type": "object", "properties": {
         "apartment_type": {"type": "string", "enum": ["rent_stabilized", "not_regulated", "rent_controlled"]},
         "lease_start_date": {"type": "string", "description": "YYYY-MM-DD start of the new lease"},
         "term_years": {"type": "integer", "enum": [1, 2]}},
         "required": ["apartment_type"], "additionalProperties": False}},
]

_FUNCTIONS: dict[str, Callable[..., dict]] = {
    "search_sources": search_sources, "get_source_section": get_source_section,
    "search_lotteries": search_lotteries, "get_lottery": get_lottery, "find_lottery": find_lottery,
    "refer_to_resource": refer_to_resource, "check_rent_rules": check_rent_rules,
}


def run_tool(name: str, arguments: dict) -> dict:
    fn = _FUNCTIONS.get(name)
    if fn is None:
        return _err("unknown_tool", f"no tool named {name!r}")
    try:
        return fn(**arguments)
    except TypeError as e:  # wrong or missing argument names from the model
        return _err("invalid_argument", str(e))
