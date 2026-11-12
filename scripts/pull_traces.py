"""Download the live site's conversation traces for review.

    uv run python scripts/pull_traces.py              # every day on the server (up to 30)
    uv run python scripts/pull_traces.py --days 3     # the last 3 days

Needs NYCHA_ADMIN_TOKEN in .env (the ADMIN_TOKEN value from Render's
Environment settings). Optional NYCHA_SITE_URL, default the Render site.
Files are saved to runs/live-traces/<date>.jsonl, one JSON object per turn
(see agent/trace.py). runs/ is ignored by git; the files contain what
visitors typed (with emails, phone numbers and SSNs already redacted), so
keep them on this machine and delete them after review.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.config import load_env  # noqa: E402

DEFAULT_SITE = "https://nyc-housing-agent.onrender.com"
SSL = ssl.create_default_context(cafile=certifi.where())


def get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "pull-traces"})
    with urllib.request.urlopen(req, timeout=60, context=SSL) as resp:
        return resp.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download live conversation traces.")
    parser.add_argument("--days", type=int, help="only the most recent N days")
    args = parser.parse_args(argv)
    load_env()
    token = os.environ.get("NYCHA_ADMIN_TOKEN", "")
    site = os.environ.get("NYCHA_SITE_URL", DEFAULT_SITE).rstrip("/")
    if not token:
        print("NYCHA_ADMIN_TOKEN is not set in .env (copy ADMIN_TOKEN from Render's Environment settings).")
        return 1
    try:
        index = json.loads(get(f"{site}/admin/traces/index", token))["files"]
    except urllib.error.HTTPError as e:
        print(f"{site} answered {e.code}. Check NYCHA_ADMIN_TOKEN matches ADMIN_TOKEN on Render "
              f"and that the latest code is deployed.")
        return 1
    if args.days:
        index = index[-args.days:]
    out = ROOT / "runs" / "live-traces"
    out.mkdir(parents=True, exist_ok=True)
    turns = 0
    for f in index:
        data = get(f"{site}/admin/traces/{f['date']}", token)
        (out / f"{f['date']}.jsonl").write_bytes(data)
        n = data.count(b"\n")
        turns += n
        print(f"{f['date']}: {n} records")
    print(f"saved {len(index)} files, {turns} records, to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
