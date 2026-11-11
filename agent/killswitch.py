"""The kill switch (SPEC.md SEC-5): one environment variable, three levels.

  - off (default, and any unset or unknown value): everything runs
  - laws-only: lottery tools are paused, e.g. when lottery data looks wrong
  - maintenance: no model calls; the page shows a static notice

The default is a no-op on purpose so an unset variable never pauses anything.
"""

from __future__ import annotations

import os

from agent.config import load_facts

LOTTERY_TOOLS = frozenset({"search_lotteries", "get_lottery", "find_lottery"})


def level() -> str:
    cfg = load_facts()["kill_switch"]
    value = os.environ.get(cfg["env_var"], "").strip().lower()
    return value if value in cfg["levels"] else "off"


def paused_reason(tool_name: str) -> str | None:
    """Why a tool is paused right now, or None."""
    current = level()
    if current == "maintenance" or (current == "laws-only" and tool_name in LOTTERY_TOOLS):
        return ("Lottery answers are paused right now. Point the user to the Housing Connect open lotteries "
                "page instead.")
    return None
