"""Usage limits and the daily spending cap (SPEC.md LIMIT-1 to LIMIT-6).

Limits live in facts.yaml. Spend is computed from each response's token usage
at the model's published price and stored per New York day, so the cap holds
across sessions and restarts.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from agent.config import NEW_YORK, load_facts

# USD per million tokens: input, output, cache write, cache read.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
}
# An unknown model (for example a refusal fallback) is charged at the highest known rates.
WORST_CASE = tuple(max(p[i] for p in PRICES.values()) for i in range(4))


def limits() -> dict:
    return load_facts()["limits"]


def cost_usd(model: str, usage) -> float:
    p_in, p_out, p_write, p_read = PRICES.get(model, WORST_CASE)
    get = lambda name: getattr(usage, name, 0) or 0  # noqa: E731
    return (get("input_tokens") * p_in + get("output_tokens") * p_out
            + get("cache_creation_input_tokens") * p_write + get("cache_read_input_tokens") * p_read) / 1e6


def _day() -> str:
    return datetime.now(NEW_YORK).date().isoformat()


def spend_today(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT usd FROM daily_spend WHERE day = ?", (_day(),)).fetchone()
    return row["usd"] if row else 0.0


def record_spend(conn: sqlite3.Connection, usd: float) -> float:
    conn.execute("INSERT INTO daily_spend(day, usd) VALUES (?, ?) "
                 "ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd", (_day(), usd))
    return spend_today(conn)


def over_daily_cap(conn: sqlite3.Connection) -> bool:
    return spend_today(conn) >= limits()["daily_model_spend_usd"]


def count_session_message(conn: sqlite3.Connection, session_id: str) -> int:
    conn.execute("INSERT INTO session_usage(session_id, messages) VALUES (?, 1) "
                 "ON CONFLICT(session_id) DO UPDATE SET messages = messages + 1", (session_id,))
    return conn.execute("SELECT messages FROM session_usage WHERE session_id = ?", (session_id,)).fetchone()[0]


def count_ip_message(conn: sqlite3.Connection, ip: str) -> int:
    hour = datetime.now(NEW_YORK).strftime("%Y-%m-%dT%H")
    conn.execute("INSERT INTO ip_usage(ip, hour, messages) VALUES (?, ?, 1) "
                 "ON CONFLICT(ip, hour) DO UPDATE SET messages = messages + 1", (ip, hour))
    return conn.execute("SELECT messages FROM ip_usage WHERE ip = ? AND hour = ?", (ip, hour)).fetchone()[0]


def check_message(conn: sqlite3.Connection, text: str, session_id: str, ip: str | None = None) -> str | None:
    """The limit a new message breaks (LIMIT-1 to LIMIT-3, LIMIT-6), or None. Counts the message if allowed."""
    lim = limits()
    if len(text) > lim["message_max_chars"]:
        return "message_too_long"
    if over_daily_cap(conn):
        return "daily_cap"
    if count_session_message(conn, session_id) > lim["messages_per_session"]:
        return "session_limit"
    if ip is not None and count_ip_message(conn, ip) > lim["messages_per_ip_per_hour"]:
        return "rate_limited"
    return None


def spend_alert(conn: sqlite3.Connection) -> bool:
    """OPS-2: daily spend has passed the alert threshold."""
    lim = limits()
    return spend_today(conn) >= lim["daily_model_spend_usd"] * lim["spend_alert_percent"] / 100
