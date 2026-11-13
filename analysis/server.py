"""Local trace viewer for error analysis (cartwheel's analysis/ method).

    uv run python -m analysis.server                 # http://127.0.0.1:8020
    uv run python -m analysis.server --port 8021

Reads traces from this machine only and listens on 127.0.0.1, never on the
public site: live traces contain what visitors typed.

Trace sources:
  run:<name>   an eval run, runs/<name>/results.jsonl (scenarios.runner)
  live         live-site traces pulled with scripts/pull_traces.py (runs/live-traces/)
  local        traces from local testing (data/traces/)

Review state lives in analysis/state/ (ignored by git, since notes quote traces):
  annotations.json      the human's notes (written only by the viewer)
  patterns.json         failure modes grouped from the notes (written by Claude, edited by the human)
  suggestions.json      suggested instances of a mode, awaiting accept or dismiss
  labels/<mode>.jsonl   present/absent decisions, append-only; the last line per trace wins
  verdicts.jsonl        the reviewer's own pass/fail per trace, append-only; the last line per trace wins
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
UI = Path(__file__).resolve().parent / "ui" / "index.html"
STATE = Path(__file__).resolve().parent / "state"
LOCK = threading.Lock()
MODE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,60}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- Sources and normalization ---------------------------------------------------


def list_sources(root: Path = ROOT) -> list[dict]:
    out = []
    for results in sorted((root / "runs").glob("*/results.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        rows = _jsonl(results)
        out.append({"id": f"run:{results.parent.name}", "label": f"Eval run: {results.parent.name}",
                    "count": len(rows)})
    for sid, folder, label in (("live", root / "runs" / "live-traces", "Live site traces"),
                               ("local", root / "data" / "traces", "Local testing traces")):
        files = sorted(folder.glob("*.jsonl"))
        if files:
            sessions = {r.get("session") for f in files for r in _jsonl(f)}
            out.append({"id": sid, "label": f"{label} ({files[0].stem} to {files[-1].stem})",
                        "count": len(sessions)})
    return out


def tool_summary(name: str, result: dict) -> str:
    """One line shown on a folded tool result."""
    if not isinstance(result, dict):
        return str(result)[:120]
    if result.get("ok") is False:
        return f"error: {result.get('error')}: {result.get('reason', '')}"[:200]
    if "results" in result:
        hits = result["results"]
        top = f" · top: {hits[0]['source_id']} › {hits[0]['title']}" if hits else ""
        return f"{len(hits)} results{top}"
    if "text" in result and "source_id" in result:
        return f"{len(result['text']):,} characters · {result.get('title') or result.get('url')}"
    if "lotteries" in result:
        below = len(result.get("below_minimum") or [])
        gap = len(result.get("income_gap") or [])
        return (f"{len(result['lotteries'])} lotteries" + (f", {gap} in an income gap" if gap else "")
                + (f", {below} below income minimum" if below else "")
                + f" · {result.get('source_mode', '?')} data as of {str(result.get('data_as_of', '?'))[:10]}")
    if "matches" in result:
        return f"{len(result['matches'])} matches: " + ", ".join(m.get("name", "?") for m in result["matches"][:3])
    if "resources" in result:
        return ", ".join(r.get("name", "?") for r in result["resources"])
    if "lottery_id" in result:
        return f"{result.get('name')} · closes {result.get('end_date')}"
    keys = [k for k in result if k != "ok"]
    return "fields: " + ", ".join(keys[:8])


def _tool(call: dict) -> dict:
    result = call.get("result")
    if result is None:  # run results before steps were recorded keep only ok/error
        result = {k: call[k] for k in ("ok", "error") if call.get(k) is not None}
    return {"name": call["name"], "input": call.get("input", {}), "result": result,
            "summary": tool_summary(call["name"], result)}


def _turn(raw: dict) -> dict:
    steps = raw.get("steps")
    if steps:
        steps = [{"thinking": s.get("thinking", ""), "text": s.get("text", ""), "stop_reason": s.get("stop_reason"),
                  "usage": s.get("usage"), "cost_usd": s.get("cost_usd"), "tools": [_tool(t) for t in s["tools"]]}
                 for s in steps]
    elif raw.get("tool_calls"):  # older traces: tool calls only, order within the turn kept, no text between
        steps = [{"thinking": "", "text": "", "tools": [_tool(t) for t in raw["tool_calls"]], "legacy": True}]
    else:
        steps = []
    return {"user": raw.get("user", ""), "reply": raw.get("reply", ""), "blocked": raw.get("blocked"),
            "removed_links": raw.get("removed_links") or [], "steps": steps,
            "model_calls": raw.get("model_calls", len(steps)), "cost_usd": raw.get("cost_usd", 0.0)}


def _meta(turns: list[dict], **extra) -> dict:
    return {"turns": len(turns), "tool_calls": sum(len(s["tools"]) for t in turns for s in t["steps"]),
            "model_calls": sum(t["model_calls"] or 0 for t in turns),
            "cost_usd": round(sum(t["cost_usd"] or 0 for t in turns), 6), **extra}


def load_traces(source: str, root: Path = ROOT) -> list[dict]:
    if source.startswith("run:"):
        name = source[4:]
        if "/" in name or name.startswith("."):
            raise ValueError("bad run name")
        traces = []
        for r in _jsonl(root / "runs" / name / "results.jsonl"):
            turns = [_turn(t) for t in r.get("turns", [])]
            traces.append({
                "key": f"{source}::{r['id']}", "id": r["id"], "source": source, "status": r.get("status"),
                "group": r.get("scenario_group"), "tuple": r.get("tuple", {}),
                "title": turns[0]["user"] if turns else "",
                "meta": _meta(turns, model=r.get("model"), prompt_version=r.get("prompt_version"),
                              seconds=r.get("seconds"), at=r.get("run_at"), as_of=r.get("as_of")),
                "expected": {"evaluation": r.get("evaluation"), "criterion": r.get("criterion"),
                             "checks": r.get("checks", []), "error": r.get("error")},
                "turns": turns})
        return traces
    folder = {"live": root / "runs" / "live-traces", "local": root / "data" / "traces"}.get(source)
    if folder is None:
        raise ValueError(f"unknown source {source!r}")
    by_session: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(folder.glob("*.jsonl")):
        for row in _jsonl(f):
            by_session[row.get("session") or "?"].append(row)
    traces = []
    for session, rows in by_session.items():
        rows.sort(key=lambda r: r.get("at", ""))
        turns = [_turn(r) for r in rows]
        blocked = [t["blocked"] for t in turns if t["blocked"]]
        traces.append({
            "key": f"{source}::{session}", "id": session[:10], "source": source,
            "status": "blocked" if blocked and len(blocked) == len(turns) else None,
            "group": None, "tuple": {}, "title": turns[0]["user"] if turns else "",
            "meta": _meta(turns, model=next((r.get("model") for r in rows if r.get("model")), None),
                          prompt_version=rows[-1].get("prompt_version"), at=rows[0].get("at")),
            "expected": None, "turns": turns})
    traces.sort(key=lambda t: t["meta"]["at"] or "", reverse=True)
    return traces


def add_outlier_flags(traces: list[dict]) -> None:
    """Flag a trace on a feature where it is above 90% of the others (only with 10+ traces)."""
    if len(traces) < 10:
        for t in traces:
            t["flags"] = []
        return
    labels = {"tool_calls": "tool calls", "model_calls": "model calls", "cost_usd": "cost", "seconds": "seconds",
              "turns": "turns"}
    for t in traces:
        t["flags"] = []
    for feature, label in labels.items():
        values = [t["meta"].get(feature) for t in traces]
        if any(v is None for v in values):
            continue
        for t in traces:
            v = t["meta"][feature]
            share = sum(1 for o in values if o < v) / len(values)
            if share > 0.9:
                shown = f"${v:.3f}" if feature == "cost_usd" else f"{v:g} {label}"
                t["flags"].append(f"{shown} (more than {round(share * 100)}% of traces)")


def summaries(traces: list[dict]) -> list[dict]:
    return [{k: t[k] for k in ("key", "id", "status", "group", "tuple", "title", "meta", "flags")} for t in traces]


# --- Review state -----------------------------------------------------------------


class State:
    def __init__(self, folder: Path = STATE):
        self.folder = folder

    def _path(self, name: str) -> Path:
        return self.folder / name

    def read(self, name: str, key: str) -> list[dict]:
        p = self._path(name)
        return json.loads(p.read_text()).get(key, []) if p.exists() else []

    def write(self, name: str, key: str, rows: list[dict]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name + ".tmp")
        tmp.write_text(json.dumps({key: rows}, indent=2) + "\n")
        tmp.replace(self._path(name))

    def labels(self) -> list[dict]:
        """Current label per (mode, trace): the last decision written."""
        current: dict[tuple[str, str], dict] = {}
        for f in sorted((self.folder / "labels").glob("*.jsonl")):
            for row in _jsonl(f):
                current[(row["mode"], row["trace_key"])] = row
        return [r for r in current.values() if r["value"] is not None]

    def snapshot(self) -> dict:
        return {"annotations": self.read("annotations.json", "annotations"),
                "modes": self.read("patterns.json", "modes"),
                "suggestions": [s for s in self.read("suggestions.json", "suggestions")
                                if s.get("status", "pending") == "pending"],
                "labels": self.labels(), "verdicts": self.verdicts()}

    def verdicts(self) -> list[dict]:
        """The reviewer's own pass/fail per trace: the last decision written."""
        current = {row["trace_key"]: row for row in _jsonl(self.folder / "verdicts.jsonl")}
        return [r for r in current.values() if r["value"] is not None]

    def verdict(self, trace_key: str, value: str | None, note: str = "") -> dict:
        if value not in ("pass", "fail", None):
            raise ValueError("verdict must be pass, fail, or null")
        self.folder.mkdir(parents=True, exist_ok=True)
        previous = next((r for r in self.verdicts() if r["trace_key"] == trace_key), None)
        row = {"trace_key": trace_key, "value": value, "note": note, "ts": now()}
        if previous and previous["value"] != value:
            row["changed_from"] = previous["value"]
        with open(self.folder / "verdicts.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def version(self) -> str:
        files = [self._path(n) for n in ("annotations.json", "patterns.json", "suggestions.json", "verdicts.jsonl")]
        files += sorted((self.folder / "labels").glob("*.jsonl"))
        return ";".join(f"{f.name}:{f.stat().st_mtime_ns}" for f in files if f.exists())

    # Each change is one small operation, so the viewer and Claude never overwrite each other's work.
    def annotate(self, op: dict) -> dict:
        rows = self.read("annotations.json", "annotations")
        if op["op"] == "add":
            row = {"id": "a-" + uuid.uuid4().hex[:8], "trace_key": op["trace_key"], "block": op["block"],
                   "start": int(op["start"]), "end": int(op["end"]), "quote": op["quote"], "note": op["note"].strip(),
                   "author": op.get("author", "human"), "ts": now()}
            for k in ("from_suggestion", "mode"):
                if op.get(k):
                    row[k] = op[k]
            rows.append(row)
        elif op["op"] == "update":
            row = next(r for r in rows if r["id"] == op["id"])
            row.update(note=op["note"].strip(), edited=now())
        elif op["op"] == "delete":
            row = {"id": op["id"]}
            rows = [r for r in rows if r["id"] != op["id"]]
        else:
            raise ValueError("unknown op")
        self.write("annotations.json", "annotations", rows)
        return row

    def decide_suggestion(self, sid: str, accept: bool, note: str | None = None) -> dict:
        rows = self.read("suggestions.json", "suggestions")
        s = next(r for r in rows if r["id"] == sid)
        s.update(status="accepted" if accept else "dismissed", decided=now())
        self.write("suggestions.json", "suggestions", rows)
        out = {"suggestion": s}
        if accept:
            out["annotation"] = self.annotate({
                "op": "add", "trace_key": s["trace_key"], "block": s["block"], "start": s.get("start", 0),
                "end": s.get("end", 0), "quote": s["quote"], "note": note or s.get("note") or s["mode"],
                "author": "accepted_suggestion", "from_suggestion": sid,
                "mode": s.get("mode")})
            if s.get("mode"):
                self.label(s["trace_key"], s["mode"], 1, f"accepted suggestion {sid}", "accepted_suggestion")
        return out

    def label(self, trace_key: str, mode: str, value: int | None, note: str = "", source: str = "human") -> dict:
        if not MODE_NAME.match(mode):
            raise ValueError("mode names are snake_case")
        if value not in (0, 1, None):
            raise ValueError("value must be 1, 0, or null")
        folder = self.folder / "labels"
        folder.mkdir(parents=True, exist_ok=True)
        previous = next((r for r in self.labels() if r["mode"] == mode and r["trace_key"] == trace_key), None)
        row = {"trace_key": trace_key, "mode": mode, "value": value, "note": note, "source": source, "ts": now()}
        if previous and previous["value"] != value:
            row["changed_from"] = previous["value"]
        with open(folder / f"{mode}.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
        return row


# --- HTTP ----------------------------------------------------------------------------


def system_prompt() -> dict:
    from agent.agent import prompt_version, render_system_prompt

    return {"version": prompt_version(), "text": render_system_prompt()}


class Handler(BaseHTTPRequestHandler):
    state = State()
    root = ROOT

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _traces(self, source: str) -> list[dict]:
        traces = load_traces(source, self.root)
        add_outlier_flags(traces)
        return traces

    def _local_host(self) -> bool:
        # Refuse requests addressed to another name (DNS rebinding): only 127.0.0.1 / localhost.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_host():
            return self._send(403, {"error": "local only"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/sources":
                return self._send(200, {"sources": list_sources(self.root)})
            if url.path == "/api/traces":
                return self._send(200, {"traces": summaries(self._traces(q["source"]))})
            if url.path == "/api/trace":
                t = next((t for t in self._traces(q["source"]) if t["key"] == q["key"]), None)
                return self._send(200 if t else 404, t or {"error": "not found"})
            if url.path == "/api/state":
                with LOCK:
                    return self._send(200, {"version": self.state.version(), **self.state.snapshot()})
            if url.path == "/api/version":
                return self._send(200, {"version": self.state.version()})
            if url.path == "/api/system_prompt":
                return self._send(200, system_prompt())
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_host():
            return self._send(403, {"error": "local only"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, {"error": "send JSON"})  # also blocks simple cross-site form posts
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}"))
        path = urlparse(self.path).path
        try:
            with LOCK:
                if path == "/api/annotations":
                    return self._send(200, self.state.annotate(body))
                if path == "/api/suggestions":
                    return self._send(200, self.state.decide_suggestion(body["id"], body["op"] == "accept",
                                                                        body.get("note")))
                if path == "/api/verdicts":
                    return self._send(200, self.state.verdict(body["trace_key"], body["value"], body.get("note", "")))
                if path == "/api/labels":
                    return self._send(200, self.state.label(body["trace_key"], body["mode"], body["value"],
                                                            body.get("note", "")))
        except (KeyError, ValueError, StopIteration) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local trace viewer.")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Trace viewer at http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
