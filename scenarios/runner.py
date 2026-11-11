"""Run scenarios against the agent and grade the results.

    uv run python -m scenarios.runner scenarios/pilot_scenarios.jsonl --max-usd 3
    uv run python -m scenarios.runner scenarios/full_scenarios.jsonl --max-usd 25 --workers 4
    uv run python -m scenarios.runner scenarios/full_scenarios.jsonl --only nyc-001 nyc-004
    uv run python -m scenarios.runner scenarios/full_scenarios.jsonl --resume     # rerun only unfinished ones
    uv run python -m scenarios.runner scenarios/full_scenarios.jsonl --regrade    # re-grade saved results, no API calls

Every scenario runs in its own world, as scenarios/README.md requires: a
fresh database loaded with the scenario's lottery fixture (or the Open Data
copy for backup-mode scenarios, or with some income tables withheld), the
clock frozen at its `as_of` date, and its own trace folder. Scenarios run in
separate processes, so their settings never mix.

Each run calls Claude and costs money. --max-usd stops starting new
scenarios once the run's spend reaches the budget. Results go to
runs/<name>/results.jsonl (one line per scenario) and runs/<name>/summary.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from scenarios.grade import grade
from scenarios.validate import load_jsonl, validate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scenarios" / "fixtures"


def run_one(scenario: dict, traces_root: str, client_factory=None) -> dict:
    """Run one scenario in a fresh world. Called in a worker process."""
    world = Path(tempfile.mkdtemp(prefix=f"nycha-{scenario['id']}-"))
    os.environ["NYCHA_DB"] = str(world / "agent.db")
    os.environ["NYCHA_TODAY"] = scenario["as_of"]
    os.environ["NYCHA_TRACES_DIR"] = str(Path(traces_root) / scenario["id"])
    os.environ.pop("NYCHOUSING_KILL_SWITCH", None)

    from agent import agent, db
    from agent.config import load_env
    from sync.lotteries import load_fixture, sync_from_opendata

    load_env()
    query = scenario["expected"].get("lottery_query") or {}
    with db.connection() as conn:
        if query.get("mode") == "fallback":
            sync_from_opendata(conn, FIXTURES / f"opendata-{scenario['as_of']}.json")
        else:
            load_fixture(conn, FIXTURES / f"lotteries-{scenario['as_of']}.json",
                         frozenset(query.get("unknown_ids", [])))

    client = client_factory() if client_factory else None
    session = agent.new_session(ip=f"eval-{scenario['id']}")
    turns, error = [], None
    started = time.monotonic()
    for message in [scenario["opening_message"], *scenario["followups"]]:
        try:
            r = agent.run_turn(session, message, client, turn_id=f"{scenario['id']}-{len(turns)}")
        except Exception as e:  # provider errors: record and stop this scenario
            error = f"{type(e).__name__}: {e}"
            break
        turns.append({"user": message, "reply": r.reply, "blocked": r.blocked, "model": r.model,
                      "stop_reason": r.stop_reason, "model_calls": r.model_calls, "cost_usd": round(r.cost_usd, 6),
                      "removed_links": r.removed_links,
                      "tool_calls": [{"name": c["name"], "input": c["input"],
                                      "ok": c["result"].get("ok"), "error": c["result"].get("error")}
                                     for c in r.tool_calls]})
    record = {"id": scenario["id"], "scenario_group": scenario["scenario_group"], "tuple": scenario["tuple"],
              "evaluation": scenario["expected"]["evaluation"],
              "criterion": scenario["expected"].get("criterion") or scenario["expected"].get("outcome"),
              "prompt_version": agent.prompt_version(), "model": next((t["model"] for t in turns if t["model"]), None),
              "cost_usd": round(sum(t["cost_usd"] for t in turns), 6), "seconds": round(time.monotonic() - started, 1),
              "turns": turns, "error": error, "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if error:
        record.update(status="error", checks=[])
    else:
        record.update(grade(scenario, turns))
    return record


def summarize(records: list[dict]) -> str:
    lines = ["# Scenario run summary", ""]
    total = sum(r["cost_usd"] for r in records)
    lines.append(f"{len(records)} scenarios, ${total:.2f} spent, prompt version "
                 f"{', '.join(sorted({r['prompt_version'] for r in records}))}, model "
                 f"{', '.join(sorted({str(r['model']) for r in records}))}.")
    lines += ["", "Pass means every code check passed. Needs review means a person must judge the reply "
              "(human-judgment scenarios, or checks code cannot decide).", ""]
    lines += ["| Group | Pass | Fail | Needs review | Error |", "| --- | --- | --- | --- | --- |"]
    for group in ("coverage", "challenge"):
        c = Counter(r["status"] for r in records if r["scenario_group"] == group)
        if c:
            lines.append(f"| {group} | {c['pass']} | {c['fail']} | {c['needs_review']} | {c['error']} |")
    checks: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        for c in r["checks"]:
            checks[c["check"]][c["status"]] += 1
    lines += ["", "## Checks", "", "| Check | Pass | Fail | Review |", "| --- | --- | --- | --- |"]
    for name, c in sorted(checks.items(), key=lambda kv: -kv[1]["fail"]):
        lines.append(f"| {name} | {c['pass']} | {c['fail']} | {c['review']} |")
    lines += ["", "## Failures by dimension value", "", "| Dimension | Value | Fail / run |", "| --- | --- | --- |"]
    for dim in ("topic", "style", "scope", "intention", "information_given", "apartment_type", "location",
                "lottery_situation", "edge_position"):
        by: dict[str, Counter] = defaultdict(Counter)
        for r in records:
            by[r["tuple"][dim]][r["status"]] += 1
        for value, c in sorted(by.items()):
            if c["fail"]:
                lines.append(f"| {dim} | {value} | {c['fail']} / {sum(c.values())} |")
    lines += ["", "## Failed scenarios", ""]
    for r in records:
        if r["status"] in ("fail", "error"):
            why = r["error"] or "; ".join(f"{c['check']}: {c['detail']}" for c in r["checks"] if c["status"] == "fail")
            lines.append(f"- **{r['id']}** ({r['scenario_group']}): {why}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and grade eval scenarios.")
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("--name", help="run folder name under runs/ (default: file stem + timestamp)")
    parser.add_argument("--only", nargs="*", help="scenario ids to run")
    parser.add_argument("--max-usd", type=float, help="stop starting scenarios once the run has spent this much")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--resume", action="store_true", help="keep finished results in the run folder, run the rest")
    parser.add_argument("--regrade", action="store_true", help="re-grade saved results without calling the model")
    args = parser.parse_args(argv)

    scenarios = load_jsonl(args.scenarios)
    if errors := validate(scenarios):
        print("Scenario file has errors; fix them first:\n  " + "\n  ".join(errors))
        return 1
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]
    by_id = {s["id"]: s for s in scenarios}

    runs = ROOT / "runs"
    if args.name:
        run_dir = runs / args.name
    elif args.resume or args.regrade:
        candidates = sorted(runs.glob(f"{args.scenarios.stem}-*"))
        if not candidates:
            print("No earlier run to resume or re-grade.")
            return 1
        run_dir = candidates[-1]
    else:
        run_dir = runs / f"{args.scenarios.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    done = {r["id"]: r for r in load_jsonl(results_path)} if results_path.exists() else {}

    if args.regrade:
        for rid, r in done.items():
            if rid in by_id and r["status"] != "error":
                r.update(grade(by_id[rid], r["turns"]))
        results_path.write_text("".join(json.dumps(r) + "\n" for r in done.values()))
        (run_dir / "summary.md").write_text(summarize(list(done.values())))
        print(f"re-graded {len(done)} results in {run_dir}")
        return 0

    if not args.resume:
        done = {}
    todo = [s for s in scenarios if s["id"] not in done or done[s["id"]]["status"] == "error"]
    print(f"{len(todo)} scenarios to run in {run_dir} ({len(done)} already done), {args.workers} at a time"
          + (f", budget ${args.max_usd:.2f}" if args.max_usd else ""))
    spent = sum(r["cost_usd"] for r in done.values())
    pending = list(todo)
    traces = str(run_dir / "traces")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        running = {}
        while pending or running:
            while pending and len(running) < args.workers and (args.max_usd is None or spent < args.max_usd):
                s = pending.pop(0)
                running[pool.submit(run_one, s, traces)] = s["id"]
            if not running:
                print(f"budget reached (${spent:.2f}); {len(pending)} scenarios not run. Use --resume to continue.")
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                sid = running.pop(fut)
                try:
                    record = fut.result()
                except Exception as e:
                    record = {"id": sid, "status": "error", "error": f"{type(e).__name__}: {e}", "checks": [],
                              "turns": [], "cost_usd": 0.0, "scenario_group": by_id[sid]["scenario_group"],
                              "tuple": by_id[sid]["tuple"], "prompt_version": "?", "model": None}
                done[sid] = record
                spent += record["cost_usd"]
                with open(results_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                print(f"{record['status']:<13} {sid}  ${record['cost_usd']:.3f}  (run total ${spent:.2f})")

    # Rewrite the results file once, keeping the latest record per scenario, in scenario order.
    order = {s["id"]: i for i, s in enumerate(load_jsonl(args.scenarios))}
    records = sorted(done.values(), key=lambda r: order.get(r["id"], 10**6))
    results_path.write_text("".join(json.dumps(r) + "\n" for r in records))
    (run_dir / "summary.md").write_text(summarize(records))
    counts = Counter(r["status"] for r in records)
    print(f"\n{dict(counts)}; ${spent:.2f} spent. Summary: {run_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
