# NYC housing agent

A Q&A agent for NYC renters: rental laws and affordable housing lotteries.
[SPEC.md](SPEC.md) is the source of intended behavior. Numbers the agent
states come from [facts.yaml](facts.yaml); the sources it may cite are listed
in [sources.yaml](sources.yaml).

## Setup

```sh
cp .env.example .env    # then add NYSENATE_API_KEY
uv sync
```

## Chat with the agent

```sh
uv run python -m sync.lotteries          # sync open lotteries from Housing Connect (one search + 2 calls per new lottery)
uv run python -m agent.cli --debug       # chat in the terminal; --debug shows tool calls and cost
```

To try it against the frozen eval data instead of today's lotteries:

```sh
uv run python -m agent.cli --debug --today 2026-11-10 --fixture scenarios/fixtures/lotteries-2026-11-10.json
```

## Run the website locally

```sh
uv run uvicorn server.app:app --port 8000      # then open http://localhost:8000
```

On startup the server syncs today's lotteries from Housing Connect, then again every 6 hours. Set
`NYCHA_FIXTURE=scenarios/fixtures/lotteries-2026-11-10.json NYCHA_TODAY=2026-11-10` to use the frozen
eval data instead. Every chat message calls Claude and counts toward the $5/day cap.

## Review live conversations

The live site logs every turn (redacted) to its disk for 30 days. To download them:

```sh
uv run python scripts/pull_traces.py            # saves runs/live-traces/<date>.jsonl
```

This needs `NYCHA_ADMIN_TOKEN` in `.env`: the `ADMIN_TOKEN` value from the Render service's
Environment settings. The files hold what visitors typed, so keep them on this machine and delete
them after review.

## Review traces

A local viewer for reading traces and writing notes on them (cartwheel's error-analysis method):

```sh
uv run python -m analysis.server       # http://127.0.0.1:8020
```

Pick a source at the top right: an eval run (`runs/<name>/`), live-site traces pulled with
`scripts/pull_traces.py`, or traces from local testing (`data/traces/`).

- **Traces**: read a whole conversation (user, thinking, tool calls with results, guards, reply).
  Select the text where the first failure shows, type a short note in your own words, press Enter.
  Expected outcomes and grader checks are folded at the bottom so they don't steer your read.
- **Failure modes**: Claude groups your notes into modes in `analysis/state/patterns.json` and
  may suggest more instances; you accept or dismiss each one.
- **Labeling**: once the modes settle, mark each mode present or absent on every reviewed trace.

Review state is kept in `analysis/state/` (ignored by git, since notes quote traces). The viewer
listens on 127.0.0.1 only and is never deployed. Traces saved before 2026-11-12 show tool calls
without the agent's text or thinking between them.

## Layout

| Path | What it is |
| --- | --- |
| `agent/agent.py` | System prompt and the turn loop (Anthropic SDK, Claude Opus 5.5). |
| `agent/tools.py` | The seven read-only tools and their definitions. |
| `agent/eligibility.py`, `agent/rent_rules.py` | Open-lottery rule, eligibility, and rent limits, decided in code. |
| `agent/guards.py`, `agent/limits.py`, `agent/killswitch.py` | Link allowlist, injection check, redaction; usage limits and the $5/day cap; kill switch. |
| `agent/sources.py`, `agent/db.py`, `agent/trace.py` | Source search, the SQLite database, local traces (30-day retention). |
| `sync/lotteries.py` | The lottery sync job (API, backup, or fixture). |
| `sources/`, `scenarios/` | Source snapshots and validation; eval scenarios. |
| `analysis/` | The local trace viewer and its review state. |

## Commands

| Command | What it does |
| --- | --- |
| `uv run python -m sources.snapshot` | Saves every law source to `data/sources/` as Markdown sections. `--only <id>` for one source. |
| `uv run python -m sources.validate` | Checks that the spec, `sources.yaml`, `facts.yaml`, and the snapshots agree, and that key numbers in `facts.yaml` appear in the saved sources. `--allow-missing` warns instead of failing for sources not yet saved. |
| `uv run --group dev pytest` | Runs the offline tests. |

## Yearly updates

- **Each summer, after the Rent Guidelines Board votes:** add the new order's
  slug to `rgb-apartment-orders` in `sources.yaml`, add the order to
  `rgb_apartment_orders` in `facts.yaml`, then snapshot and validate.
- **Each August, after DHCR's Good Cause notice:** update `notice_pdf` for
  `dhcr-good-cause` in `sources.yaml`, add the new figures to `good_cause` in
  `facts.yaml`, then snapshot and validate.
