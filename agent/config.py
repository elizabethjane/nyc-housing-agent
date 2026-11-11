"""Shared paths, the facts sheet, and the clock.

Paths and the date resolve from environment variables first so tests and the
scenario runner can point the agent at a temporary world:

  - NYCHA_DB: SQLite database (default data/agent.db)
  - NYCHA_SOURCES_DIR: saved law sources (default data/sources)
  - NYCHA_TODAY: freeze "today" (YYYY-MM-DD), used by the scenario runner
  - NYCHA_MODEL: Claude model id (default claude-opus-5-5)
"""

from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_MODEL = "claude-opus-5-5"


def db_path() -> Path:
    return Path(os.environ.get("NYCHA_DB", REPO_ROOT / "data" / "agent.db"))


def sources_dir() -> Path:
    return Path(os.environ.get("NYCHA_SOURCES_DIR", REPO_ROOT / "data" / "sources"))


def model_name() -> str:
    return os.environ.get("NYCHA_MODEL", DEFAULT_MODEL)


def today() -> date:
    """Today in New York time, or the frozen date in NYCHA_TODAY."""
    frozen = os.environ.get("NYCHA_TODAY")
    return date.fromisoformat(frozen) if frozen else datetime.now(NEW_YORK).date()


@lru_cache(maxsize=1)
def load_facts() -> dict:
    return yaml.safe_load((REPO_ROOT / "facts.yaml").read_text())


@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict]:
    """sources.yaml, keyed by source id."""
    return {s["id"]: s for s in yaml.safe_load((REPO_ROOT / "sources.yaml").read_text())["sources"]}


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=value lines from .env without overriding the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(name.strip(), value.strip())
