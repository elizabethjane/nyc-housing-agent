"""The public web app: chat page and API (SPEC.md section 8).

    uv run uvicorn server.app:app --reload --port 8000

Environment:
  - SESSION_SECRET: signs the anonymous session cookie (HOST-1). Required in
    production; a random one is generated for local runs.
  - NYCHA_FIXTURE: load a frozen lottery fixture at startup instead of syncing
    (local testing and evals). With it set, the scheduled sync does not run.
  - NYCHA_SYNC_DISABLED=1: skip the scheduled sync.
  - TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY: Cloudflare Turnstile bot check
    (LIMIT-8). With both set, a session must pass the check before its first
    model call. Without them the check is off (local runs).
  - RENDER (set by Render): marks production. There SESSION_SECRET is required,
    and the client IP is the address Render's proxy appended to
    X-Forwarded-For, not one the visitor could have sent.

Visitors get an anonymous session cookie on their first message. Sessions
live in memory, so a restart starts conversations over. One process serves
the page, the API, and the lottery sync (HOST-2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import db, trace
from agent.agent import Session, new_session, prompt_version, run_turn
from agent.config import load_env, load_facts

load_env()
log = logging.getLogger("nycha.server")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "nycha_session"
SESSION_TTL_SECONDS = 6 * 3600
PRODUCTION = bool(os.environ.get("RENDER"))
if PRODUCTION and not os.environ.get("SESSION_SECRET"):
    raise RuntimeError("SESSION_SECRET must be set in production")
SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_hex(32)).encode()
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_ON = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_verified_sessions: set[str] = set()

_sessions: dict[str, tuple[Session, float, threading.Lock]] = {}
_sessions_lock = threading.Lock()


# --- Lottery sync (GROUND-6) ----------------------------------------------------


def _sync_once() -> None:
    from sync.lotteries import sync_from_api, sync_from_opendata

    cfg = load_facts()["lottery_sync"]
    with db.connection() as conn:
        try:
            n = sync_from_api(conn)
            log.info("lottery sync: %d open lotteries", n)
        except Exception:
            log.exception("lottery sync from Housing Connect failed")
            last = db.get_meta(conn, "last_success_at")
            age_hours = (time.time() - _parse_time(last)) / 3600 if last else float("inf")
            if age_hours > cfg["api_failure_fallback_after_hours"]:
                log.warning("switching to the Open Data backup (GROUND-7)")
                sync_from_opendata(conn)


def _parse_time(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def _sync_loop(stop: threading.Event) -> None:
    hours = load_facts()["lottery_sync"]["sync_interval_hours"]
    while not stop.is_set():
        _sync_once()
        stop.wait(hours * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    if fixture := os.environ.get("NYCHA_FIXTURE"):
        from sync.lotteries import load_fixture

        with db.connection() as conn:
            log.info("loaded %d lotteries from fixture %s", load_fixture(conn, Path(fixture)), fixture)
    elif os.environ.get("NYCHA_SYNC_DISABLED") != "1":
        threading.Thread(target=_sync_loop, args=(stop,), daemon=True, name="lottery-sync").start()
    yield
    stop.set()


app = FastAPI(title="NYC Housing Agent", lifespan=lifespan, docs_url=None, redoc_url=None)


# --- Sessions (HOST-1) ----------------------------------------------------------------


def _sign(session_id: str) -> str:
    mac = hmac.new(SECRET, session_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{session_id}.{mac}"


def _verify(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    session_id, _ = cookie.split(".", 1)
    return session_id if hmac.compare_digest(_sign(session_id), cookie) else None


def _client_ip(request: Request) -> str:
    """The visitor's IP for rate limiting (LIMIT-3).

    Behind Render's proxy, the rightmost X-Forwarded-For entry is the address
    the proxy saw; earlier entries come from the visitor and can be faked.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if PRODUCTION and forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _turnstile_ok(token: str | None, ip: str) -> bool:
    """Verify a Cloudflare Turnstile token (LIMIT-8)."""
    if not token:
        return False
    data = urllib.parse.urlencode({"secret": TURNSTILE_SECRET_KEY, "response": token, "remoteip": ip}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TURNSTILE_VERIFY_URL, data=data), timeout=10) as resp:
            return bool(json.load(resp).get("success"))
    except Exception:
        log.exception("Turnstile verification failed")
        return False


def _get_session(request: Request) -> tuple[Session, threading.Lock, bool]:
    now = time.time()
    with _sessions_lock:
        for sid in [s for s, (_, seen, _) in _sessions.items() if now - seen > SESSION_TTL_SECONDS]:
            del _sessions[sid]
            _verified_sessions.discard(sid)
        sid = _verify(request.cookies.get(COOKIE))
        if sid and sid in _sessions:
            session, _, lock = _sessions[sid]
            _sessions[sid] = (session, now, lock)
            return session, lock, False
        session = new_session(ip=_client_ip(request))
        lock = threading.Lock()
        _sessions[session.id] = (session, now, lock)
        return session, lock, True


def _set_cookie(response: Response, request: Request, session: Session) -> None:
    response.set_cookie(COOKIE, _sign(session.id), httponly=True, samesite="lax",
                        secure=PRODUCTION or request.url.scheme == "https", max_age=SESSION_TTL_SECONDS)


# --- API -----------------------------------------------------------------------------


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)  # LIMIT-1 is enforced in agent.limits
    turnstile_token: str | None = None


class FeedbackIn(BaseModel):
    turn_id: str
    rating: str = Field(pattern="^(up|down)$")


@app.post("/api/chat")
def chat(body: ChatIn, request: Request) -> JSONResponse:
    session, lock, is_new = _get_session(request)
    session.ip = _client_ip(request)
    if TURNSTILE_ON and session.id not in _verified_sessions:
        if not _turnstile_ok(body.turnstile_token, session.ip):
            response = JSONResponse({"error": "bot_check", "reply": "Please complete the check above, then send "
                                     "your message again."}, status_code=403)
            if is_new:
                _set_cookie(response, request, session)
            return response
        _verified_sessions.add(session.id)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "busy", "reply": "Still working on your last message."}, status_code=409)
    turn_id = uuid.uuid4().hex
    try:
        before = len(session.messages)
        try:
            result = run_turn(session, body.message.strip(), turn_id=turn_id)
        except Exception:
            del session.messages[before:]  # keep the history valid for the next message
            log.exception("turn failed")
            return JSONResponse({"error": "server_error",
                                 "reply": "Sorry, something went wrong on our side. Please try again."},
                                status_code=502)
    finally:
        lock.release()
    status = 429 if result.blocked == "rate_limited" else 200
    response = JSONResponse({"turn_id": turn_id, "reply": result.reply, "blocked": result.blocked},
                            status_code=status)
    if is_new:
        _set_cookie(response, request, session)
    return response


@app.post("/api/feedback")
def feedback(body: FeedbackIn, request: Request) -> dict:
    session_id = _verify(request.cookies.get(COOKIE))
    trace.write({"session": session_id, "turn_id": body.turn_id, "event": "feedback", "rating": body.rating,
                 "prompt_version": prompt_version()})
    return {"ok": True}


@app.post("/api/new")
def new_conversation(request: Request) -> Response:
    sid = _verify(request.cookies.get(COOKIE))
    with _sessions_lock:
        _sessions.pop(sid, None)
    _verified_sessions.discard(sid)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response


@app.get("/api/status")
def status() -> dict:
    """Public status for the page: lottery data freshness, limits, and the bot-check site key."""
    with db.connection() as conn:
        return {"data_as_of": db.get_meta(conn, "data_as_of"), "source_mode": db.get_meta(conn, "source_mode"),
                "max_chars": load_facts()["limits"]["message_max_chars"],
                "turnstile_site_key": TURNSTILE_SITE_KEY if TURNSTILE_ON else None}


CSP = ("default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
       "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "frame-src https://challenges.cloudflare.com; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# --- Pages ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/about")
def about() -> FileResponse:
    return FileResponse(STATIC / "about.html")


@app.get("/privacy")
def privacy() -> FileResponse:
    return FileResponse(STATIC / "privacy.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
