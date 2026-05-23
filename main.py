"""
Rowdy the Homework Helper — standalone web POC.

A simple chat UI backed by Claude. No authentication, no Canvas LTI; this
exists to validate the tutor's behavior, prompt, and UX before wrestling
with LMS integration.

Each browser session gets its own conversation, scoped by a session cookie.
Conversations live in RAM and disappear on server restart.

Endpoints:
  GET   /             — chat UI
  POST  /api/chat     — chat with Rowdy (multipart: message + optional files)
  POST  /api/reset    — clear this browser's conversation
  GET   /admin/stats  — usage aggregates (requires ADMIN_TOKEN)
  GET   /healthz      — health check
"""
import base64
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from claude_service import ClaudeTutor

load_dotenv()
BASE_DIR = Path(__file__).parent

app = FastAPI(title="Rowdy — Homework Helper (POC)")

# Signed session cookies identify each browser. Lax SameSite is fine since
# we're not iframed; HTTPS-only is off here so local `uvicorn` works without
# TLS. Railway terminates TLS in front of the app.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    https_only=False,
    same_site="lax",
)

(BASE_DIR / "static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

tutor = ClaudeTutor(model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"))

# In-RAM conversation store, keyed by browser session id.
CONVERSATIONS: dict[str, list[dict]] = {}

# Lightweight per-session metadata (last activity + today's message count).
# Kept separate from CONVERSATIONS so eviction and caps don't touch history.
SESSION_META: dict[str, dict] = {}

# Process-lifetime usage aggregates (reset on restart). Metadata only.
USAGE = {
    "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "web_searches": 0,
}
# Global daily budget tracker (UTC day).
DAILY = {"day": None, "tokens": 0}

HISTORY_TURN_CAP = 12
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_DOCUMENT_TYPES = {"application/pdf"}

# --- Tunable guards (all safe/permissive by default; set via env on Railway) ---
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "8000"))
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "180"))
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "0"))            # 0 = unlimited
PER_SESSION_DAILY_MSG_CAP = int(os.environ.get("PER_SESSION_DAILY_MSG_CAP", "0"))  # 0 = unlimited
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")                                # unset = /admin/stats disabled

_BUDGET_MSG = (
    "Whoa — looks like I've been mighty popular today and need to rest up so we "
    "don't run over budget. Try me again tomorrow, or reach out to "
    "tutoring@crowder.edu for help in the meantime."
)
_SESSION_MSG = (
    "We've sure covered a lot on this device today! Let's pick it back up "
    "tomorrow. If you need more help before then, email tutoring@crowder.edu "
    "and they'll get you taken care of."
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _roll_daily() -> None:
    d = _today()
    if DAILY["day"] != d:
        DAILY["day"] = d
        DAILY["tokens"] = 0


def _evict_stale() -> None:
    """Drop sessions idle longer than the TTL so RAM stays bounded."""
    if SESSION_TTL_MINUTES <= 0:
        return
    cutoff = time.time() - SESSION_TTL_MINUTES * 60
    for sid in [s for s, m in SESSION_META.items() if m.get("last", 0) < cutoff]:
        SESSION_META.pop(sid, None)
        CONVERSATIONS.pop(sid, None)


def _touch_session(sid: str) -> dict:
    """Get/refresh this session's metadata, rolling the daily counter over."""
    d = _today()
    m = SESSION_META.get(sid)
    if m is None:
        m = {"last": time.time(), "msgs_today": 0, "day": d}
        SESSION_META[sid] = m
    if m["day"] != d:
        m["day"] = d
        m["msgs_today"] = 0
    m["last"] = time.time()
    return m


def _account(sid: str, meta: dict, usage: dict, msg_chars: int) -> None:
    """Record usage (metadata only — never prompt/response content)."""
    USAGE["requests"] += 1
    USAGE["input_tokens"] += usage["input_tokens"]
    USAGE["output_tokens"] += usage["output_tokens"]
    USAGE["cache_read_tokens"] += usage["cache_read_input_tokens"]
    USAGE["cache_write_tokens"] += usage["cache_creation_input_tokens"]
    USAGE["web_searches"] += usage["web_search_requests"]
    DAILY["tokens"] += usage["input_tokens"] + usage["output_tokens"]
    meta["msgs_today"] += 1
    print("USAGE " + json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sid": sid[:8],                       # short anonymous label, not the full id
        "in": usage["input_tokens"],
        "out": usage["output_tokens"],
        "cache_read": usage["cache_read_input_tokens"],
        "web_search": usage["web_search_requests"],
        "msg_chars": msg_chars,
    }), flush=True)


def _ensure_session_id(request: Request) -> str:
    """Assign a stable random id to this browser if it doesn't have one yet."""
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(16)
        request.session["sid"] = sid
    return sid


@app.get("/")
async def index(request: Request):
    _ensure_session_id(request)
    return templates.TemplateResponse(request, "chat.html")


@app.post("/api/chat")
async def chat(
    request: Request,
    message: str = Form(""),
    calc_log: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    sid = _ensure_session_id(request)
    _roll_daily()
    _evict_stale()
    meta = _touch_session(sid)

    if not message.strip() and not files:
        raise HTTPException(400, "Send a message or attach a file")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(413, f"Message too long (limit {MAX_MESSAGE_CHARS} characters). Try sending a shorter chunk.")

    # Soft caps surface as a friendly in-chat reply rather than an error.
    if DAILY_TOKEN_BUDGET > 0 and DAILY["tokens"] >= DAILY_TOKEN_BUDGET:
        return {"reply": _BUDGET_MSG}
    if PER_SESSION_DAILY_MSG_CAP > 0 and meta["msgs_today"] >= PER_SESSION_DAILY_MSG_CAP:
        return {"reply": _SESSION_MSG}

    content_blocks: list[dict] = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{f.filename} exceeds 5 MB limit")
        media_type = (f.content_type or "").lower()
        b64 = base64.standard_b64encode(data).decode("ascii")
        if media_type in ALLOWED_DOCUMENT_TYPES:
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            })
        elif media_type in ALLOWED_IMAGE_TYPES:
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        else:
            raise HTTPException(415, f"Unsupported file type: {media_type or 'unknown'}")
    if calc_log.strip():
        content_blocks.append({
            "type": "text",
            "text": (
                "[Calculator activity since the student's last message — for "
                "your diagnostic awareness only. These are the steps the student "
                "entered into the on-screen scientific calculator, with the angle "
                "mode and result. Never read these results back as answers; use "
                "them only to spot HOW they are using the calculator "
                "(parentheses, order of operations, DEG/RAD mode, function "
                "entry):\n" + calc_log
            ),
        })
    if message.strip():
        content_blocks.append({"type": "text", "text": message})

    history = CONVERSATIONS.setdefault(sid, [])
    history.append({"role": "user", "content": content_blocks})

    trimmed = _strip_old_attachments(history[-HISTORY_TURN_CAP:])
    try:
        reply, usage = await tutor.reply(history=trimmed)
    except Exception as exc:  # API overloaded / rate-limited / transient
        history.pop()  # don't leave a dangling user turn in history
        print(f"CHAT_ERROR {type(exc).__name__}: {exc}", flush=True)
        return {"reply": (
            "Well, shoot — somethin' hiccuped on my end. Give it another try in "
            "a sec, and if it keeps actin' up you can email tutoring@crowder.edu."
        )}
    history.append({"role": "assistant", "content": reply})

    _account(sid, meta, usage, len(message))
    return {"reply": reply}


def _strip_old_attachments(history: list[dict]) -> list[dict]:
    """Keep image/document blocks only on the most recent message.

    Documents are expensive — a 5-page PDF is ~10–15k tokens. Re-sending
    one across 12 turns of history would cost 6× more than necessary.
    Older messages get a small text placeholder instead.
    """
    if not history:
        return history
    last_idx = len(history) - 1
    cleaned = []
    for i, msg in enumerate(history):
        if i == last_idx or isinstance(msg["content"], str):
            cleaned.append(msg)
            continue
        new_blocks = []
        attachment_count = 0
        for block in msg["content"]:
            t = block.get("type") if isinstance(block, dict) else None
            if t in ("image", "document"):
                attachment_count += 1
            else:
                new_blocks.append(block)
        if attachment_count:
            new_blocks.append({
                "type": "text",
                "text": f"[{attachment_count} earlier attachment(s) — not re-sent]",
            })
        cleaned.append({"role": msg["role"], "content": new_blocks or msg["content"]})
    return cleaned


@app.post("/api/reset")
async def reset(request: Request):
    """Clear this browser's conversation so the next message starts fresh
    (Rowdy re-greets from a clean slate)."""
    sid = _ensure_session_id(request)
    CONVERSATIONS.pop(sid, None)
    return {"ok": True}


@app.get("/admin/stats")
async def admin_stats(request: Request):
    """Usage aggregates (metadata only). Requires ADMIN_TOKEN to be set and
    supplied via ?token=... or an X-Admin-Token header. Disabled if unset."""
    supplied = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    if not ADMIN_TOKEN or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(403, "Forbidden")
    _roll_daily()
    return {
        "since": USAGE["started"],
        "requests": USAGE["requests"],
        "input_tokens": USAGE["input_tokens"],
        "output_tokens": USAGE["output_tokens"],
        "cache_read_tokens": USAGE["cache_read_tokens"],
        "cache_write_tokens": USAGE["cache_write_tokens"],
        "web_searches": USAGE["web_searches"],
        "active_sessions": len(SESSION_META),
        "today": {
            "day": DAILY["day"],
            "tokens": DAILY["tokens"],
            "budget": DAILY_TOKEN_BUDGET or None,
        },
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}
