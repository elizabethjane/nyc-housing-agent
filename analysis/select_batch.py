"""Pick a varied batch of traces from an eval run for the next review pass.

    uv run python -m analysis.select_batch full-1              # 16 for coverage + 8 at random
    uv run python -m analysis.select_batch full-1 --size 30 --random 10

Reading traces in run order shows mostly the common cases, so this picks for
variety, as cartwheel's select_traces does: first, traces that add the most
not-yet-covered values across the scenario dimensions and outcome features
(grader status, turn count, tool use); then random picks, because no feature
list captures everything. Traces already reviewed (a note or a verdict) are
skipped. Writes analysis/state/samples.json, which the viewer shows as the
"Review batch" filter with each trace's reason.

A cluster or an outlier is a reason to read a trace, never a failure by itself.
"""

from __future__ import annotations

import argparse
import json
import random

from analysis.server import STATE, ROOT, State, load_traces

DIMENSIONS = ("topic", "style", "scope", "intention", "information_given", "apartment_type", "location",
              "lottery_situation", "edge_position")


def features(t: dict) -> set[tuple[str, str]]:
    f = {(d, str(t["tuple"].get(d))) for d in DIMENSIONS if t["tuple"].get(d) not in (None, "none", "not_applicable")}
    m = t["meta"]
    f |= {("status", str(t["status"])), ("group", str(t["group"])),
          ("turns", "multi" if m["turns"] > 1 else "single"),
          ("tools", "none" if m["tool_calls"] == 0 else "many" if m["tool_calls"] >= 4 else "some")}
    return f


def select(traces: list[dict], reviewed: set[str], size: int, n_random: int, seed: int) -> list[dict]:
    pool = [t for t in traces if t["key"] not in reviewed]
    covered: set[tuple[str, str]] = set()
    picked: list[dict] = []
    for _ in range(min(size - n_random, len(pool))):
        best = max(pool, key=lambda t: (len(features(t) - covered), t["status"] == "fail", t["id"]))
        new = sorted(features(best) - covered)
        if not new:
            break
        picked.append({"key": best["key"], "reason": "variety: adds " + ", ".join(f"{d}={v}" for d, v in new[:4])
                       + (f" (+{len(new) - 4} more)" if len(new) > 4 else "")})
        covered |= features(best)
        pool.remove(best)
    rng = random.Random(seed)
    for t in rng.sample(pool, min(size - len(picked), len(pool))):
        picked.append({"key": t["key"], "reason": "random pick"})
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pick a varied review batch from an eval run.")
    parser.add_argument("run", help="run folder name under runs/, e.g. full-1")
    parser.add_argument("--size", type=int, default=24)
    parser.add_argument("--random", type=int, default=8, help="how many of the batch are random picks")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if not (ROOT / "runs" / args.run / "results.jsonl").exists():
        print(f"No results for run {args.run!r}.")
        return 1
    traces = load_traces(f"run:{args.run}")
    snap = State().snapshot()
    reviewed = {a["trace_key"] for a in snap["annotations"]} | {v["trace_key"] for v in snap["verdicts"]}
    batch = select(traces, reviewed, args.size, args.random, args.seed)
    STATE.mkdir(parents=True, exist_ok=True)
    out = {"source": f"run:{args.run}", "method": f"{args.size - args.random} for variety, {args.random} random, "
           f"seed {args.seed}; already-reviewed traces skipped", "samples": batch}
    (STATE / "samples.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"{len(batch)} traces from run:{args.run} -> analysis/state/samples.json")
    for b in batch:
        print(f"  {b['key'].split('::')[1]:<10} {b['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
