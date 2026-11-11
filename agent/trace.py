"""Local run traces (SPEC.md OPS-1, PRIV-2).

One JSON line per turn in data/traces/<New York date>.jsonl: the redacted
user message and reply, tool calls and results, model, prompt version,
token usage, cost, and any limit or guard that fired. Files older than the
retention period are deleted on each write.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from agent.config import NEW_YORK, REPO_ROOT, load_facts
from agent.guards import redact


def traces_dir() -> Path:
    return Path(os.environ.get("NYCHA_TRACES_DIR", REPO_ROOT / "data" / "traces"))


def _scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def write(record: dict) -> None:
    directory = traces_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(NEW_YORK)
    with open(directory / f"{now.date().isoformat()}.jsonl", "a") as f:
        f.write(json.dumps({"at": now.isoformat(timespec="seconds"), **_scrub(record)}, default=str) + "\n")
    keep = timedelta(days=load_facts()["privacy"]["log_retention_days"])
    for old in directory.glob("*.jsonl"):
        try:
            if now.date() - datetime.fromisoformat(old.stem).date() > keep:
                old.unlink()
        except ValueError:
            continue
