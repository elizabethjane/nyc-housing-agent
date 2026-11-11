"""Chat with the agent in the terminal.

    uv run python -m agent.cli
    uv run python -m agent.cli --debug                     # show tool calls and cost per turn
    uv run python -m agent.cli --today 2026-11-10 --fixture scenarios/fixtures/lotteries-2026-11-10.json

Needs ANTHROPIC_API_KEY in .env. Lottery answers need synced data: run
`uv run python -m sync.lotteries` first, or pass --fixture to use a frozen
copy (loaded into a separate database so the real one is untouched).
Type /new to start a new conversation, /quit to exit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

try:  # arrow keys and history in input()
    import readline  # noqa: F401
except ImportError:
    pass

from agent.config import load_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with NYC Housing Agent.")
    parser.add_argument("--debug", action="store_true", help="print tool calls, model and cost")
    parser.add_argument("--today", help="freeze today's date (YYYY-MM-DD)")
    parser.add_argument("--fixture", type=Path, help="use a frozen lottery fixture")
    args = parser.parse_args(argv)

    load_env()
    if args.today:
        os.environ["NYCHA_TODAY"] = args.today
    if args.fixture:
        os.environ["NYCHA_DB"] = str(Path(tempfile.mkdtemp()) / "agent.db")
        from agent import db
        from sync.lotteries import load_fixture
        with db.connection() as conn:
            print(f"loaded {load_fixture(conn, args.fixture)} lotteries from {args.fixture.name}")
    import anthropic

    from agent.agent import new_session, run_turn

    session = new_session()
    print("NYC Housing Agent. Information, not legal advice. /new for a new conversation, /quit to exit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/quit":
            return 0
        if text == "/new":
            session = new_session()
            print("(new conversation)\n")
            continue
        before = len(session.messages)
        try:
            result = run_turn(session, text)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            print("Could not sign in to the Anthropic API. Add ANTHROPIC_API_KEY to .env (see .env.example), "
                  "or run `ant auth login`.", file=sys.stderr)
            return 1
        except anthropic.APIStatusError as e:
            print(f"API error {e.status_code}: {e.message}. Try again.", file=sys.stderr)
            del session.messages[before:]  # roll back the unfinished turn so the history stays valid
            continue
        except anthropic.APIConnectionError:
            print("Network error. Check your connection and try again.", file=sys.stderr)
            del session.messages[before:]
            continue
        if args.debug:
            for call in result.tool_calls:
                print(f"  [tool] {call['name']}({json.dumps(call['input'])}) -> "
                      f"{json.dumps(call['result'], default=str)[:300]}")
            print(f"  [model] {result.model} stop={result.stop_reason} calls={result.model_calls} "
                  f"cost=${result.cost_usd:.4f}" + (f" blocked={result.blocked}" if result.blocked else "")
                  + (f" removed_links={result.removed_links}" if result.removed_links else ""))
        print(f"\nagent> {result.reply}\n")


if __name__ == "__main__":
    sys.exit(main())
