"""ComVoice: upload a legal document and ask about it in plain words.

Run locally:  python -m uvicorn app:app --reload
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True, encoding="utf-8-sig")

import ai  # noqa: E402  (must load after dotenv so the API key is visible)
import llm  # noqa: E402
import links  # noqa: E402
import voice  # noqa: E402
from documents import (  # noqa: E402
    DATA,
    MAX_UPLOAD_BYTES,
    DocumentError,
    Extracted,
    LinkRef,
    Page,
    Source,
    load_config,
    load_sample,
    read_upload,
    to_source,
)

STATIC = ROOT / "static"

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Limits. Raise them in .env if you need to. 0 turns the request limits off.
MAX_QUESTION_CHARS = _int_env("MAX_QUESTION_CHARS", 4000)  # per question
ASK_LIMIT_PER_MIN = _int_env("ASK_LIMIT_PER_MIN", 60)  # questions per minute per person (0 = no limit)
TTS_LIMIT_PER_MIN = _int_env("TTS_LIMIT_PER_MIN", 30)  # answers read aloud per minute per person
STT_LIMIT_PER_MIN = _int_env("STT_LIMIT_PER_MIN", 30)  # recordings transcribed per minute per person
HISTORY_TURNS = 6  # earlier questions the AI sees, so follow-ups make sense

app = FastAPI(title="ComVoice", docs_url=None, redoc_url=None)
CONFIG = load_config()
OFFICIAL = CONFIG.get("official_suffixes", [])
MAX_LINKED_CHARS = 150_000


# ------------------------------------------------------------------ messages

REFUSAL = (
    "I can't find that in your document. I only answer from what it says, so I won't guess. "
    "You could ask it another way, or check with the person or office that gave you the document."
)
REFUSAL_LINKED = (
    "I can't find that in your document or in the pages linked from it. I only answer from what they say, so I won't guess. "
    "You could ask it another way, or check with the person or office that gave you the document."
)
TOO_BIG = f"That file is too big. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
NEED_DOCUMENT = "Add a document first. Tap the + button to upload a PDF, Word or text file."


# ------------------------------------------------------------- rate limiting

class RateLimiter:
    def __init__(self, limit: int, window: int) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        if self.limit <= 0:  # 0 means no limit
            return
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(429, "You're going very fast. Please wait a minute and try again.")
            q.append(now)
            if len(self.hits) > 5000:
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]


ask_limiter = RateLimiter(limit=ASK_LIMIT_PER_MIN, window=60)
upload_limiter = RateLimiter(limit=8, window=60)
link_limiter = RateLimiter(limit=6, window=60)
tts_limiter = RateLimiter(limit=TTS_LIMIT_PER_MIN, window=60)
stt_limiter = RateLimiter(limit=STT_LIMIT_PER_MIN, window=60)


def client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------ workspaces
# A workspace is one person's uploaded document (plus pages linked from it).
# It lives in memory only, for up to 24 hours. Nothing is written to disk.

@dataclass
class Workspace:
    main: Source
    name: str
    links: list[LinkRef]
    linked: list[links.FetchedPage] = field(default_factory=list)
    linked_source: Source | None = None
    created: float = field(default_factory=time.time)


SESSION_TTL = 24 * 3600
MAX_SESSIONS = 300
SESSIONS: dict[str, Workspace] = {}
SESSION_LOCK = threading.Lock()
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def clean_session_id(value: str | None) -> str:
    if not value or not SESSION_ID.match(value):
        raise HTTPException(400, "Missing or invalid session.")
    return value


def store_workspace(session_id: str, ws: Workspace) -> None:
    now = time.time()
    with SESSION_LOCK:
        for sid in [s for s, w in SESSIONS.items() if now - w.created > SESSION_TTL]:
            del SESSIONS[sid]
        if len(SESSIONS) >= MAX_SESSIONS and session_id not in SESSIONS:
            oldest = min(SESSIONS, key=lambda s: SESSIONS[s].created)
            del SESSIONS[oldest]
        SESSIONS[session_id] = ws


def get_workspace(session_id: str | None) -> Workspace | None:
    if not session_id or not SESSION_ID.match(session_id):
        return None
    with SESSION_LOCK:
        ws = SESSIONS.get(session_id)
    if ws and time.time() - ws.created > SESSION_TTL:
        return None
    return ws


def build_linked_source(fetched: list[links.FetchedPage]) -> Source | None:
    pages: list[Page] = []
    total = 0
    n = 1
    for f in fetched:
        for p in f.pages:
            if total + len(p.text) > MAX_LINKED_CHARS:
                break
            pages.append(Page(n, p.text, origin=f.title, url=f.url, local=p.local or p.number, official=f.official))
            total += len(p.text)
            n += 1
    return Source("Linked pages", "linked", pages, label="part") if pages else None


# --------------------------------------------------------------------- models

class Turn(BaseModel):
    q: str = Field("", max_length=MAX_QUESTION_CHARS)
    a: str = Field("", max_length=4000)


class AskBody(BaseModel):
    question: str = Field(..., min_length=2, max_length=MAX_QUESTION_CHARS)
    session_id: str | None = None
    history: list[Turn] = Field(default_factory=list, max_length=HISTORY_TURNS)


class SessionBody(BaseModel):
    session_id: str


class FollowBody(BaseModel):
    session_id: str
    urls: list[str] | None = Field(None, max_length=50)


class LinkBody(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    session_id: str


class FeedbackBody(BaseModel):
    rating: str = Field(..., pattern="^(clear|unclear)$")
    reason: str | None = Field(None, pattern="^(too_hard|not_asked|looks_wrong)$")
    origin: str | None = Field(None, pattern="^(main|linked|general|none)$")


class TtsBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=voice.MAX_TTS_CHARS)
    language: str | None = Field(None, pattern="^[a-z]{2,3}$")  # ISO 639 code, optional


# --------------------------------------------------------------------- helpers

def link_info(ref: LinkRef) -> dict:
    host = urlparse(ref.url).hostname or ""
    return {"url": ref.url, "host": host, "official": links.host_is_official(host, OFFICIAL), "page": ref.page}


def source_card(source: Source, page_no: int, quote: str) -> dict:
    page = source.page(page_no)
    if source.kind == "main":
        return {"kind": "main", "page": page_no, "printed": page.printed if page else None, "label": source.label, "name": source.name, "quote": quote}
    return {
        "kind": "linked",
        "page": page.local if page else page_no,
        "label": "section",
        "name": page.origin if page else "Linked page",
        "url": page.url if page else None,
        "official": bool(page and page.official),
        "quote": quote,
    }


def append_line(filename: str, record: dict) -> None:
    try:
        with open(DATA / filename, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # read-only hosting: feedback is a nice-to-have, never fail the request


def process_document(session_id: str, ex: Extracted) -> dict:
    source = to_source(ex)
    overview = None
    ai_error = None
    try:
        overview = ai.overview(source)
    except ai.AIUnavailable as exc:
        ai_error = str(exc)
    store_workspace(session_id, Workspace(main=source, name=ex.name, links=ex.links))
    return {
        "name": ex.name,
        "count": len(ex.pages),
        "label": ex.label,
        "truncated": ex.truncated,
        "links": [link_info(l) for l in ex.links],
        "overview": overview,
        "ai_error": ai_error,
    }


# --------------------------------------------------------------------- routes

def key_problem() -> str:
    """Say in plain words why no API key was found (never prints the key itself)."""
    env_var = "GEMINI_API_KEY"
    if (ROOT / ".env.txt").exists() and not (ROOT / ".env").exists():
        return "Your key file is named .env.txt. Rename it to exactly .env (no .txt), then restart the app."
    if not (ROOT / ".env").exists():
        return f"There is no file named .env next to app.py. Create it with one line: {env_var}=your-key, then restart the app."
    return f"The .env file was found, but it has no {env_var} line with a value. Add it on one line with no quotes, then restart the app."


@app.get("/api/health")
def health() -> dict:
    connected = ai.has_key()
    return {
        "ok": True,
        "ai_connected": connected,
        "hint": "" if connected else key_problem(),
        "provider": llm.provider_label(),
        "app_name": CONFIG.get("app_name"),
        "tagline": CONFIG.get("tagline"),
        "max_question_chars": MAX_QUESTION_CHARS,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "tts": voice.has_key(),  # answers read aloud by ElevenLabs (else the browser's own voice)
        "stt": voice.has_key(),  # hold-to-talk transcribed by ElevenLabs (else the browser's own recognition)
        "voice_provider": voice.voice_label(),
        "max_tts_chars": voice.MAX_TTS_CHARS,
    }


print(f"ComVoice: provider={llm.provider_label()}, API key found: {'yes' if ai.has_key() else 'NO'}" + ("" if ai.has_key() else f"  ({key_problem()})") + f"; voice: {voice.voice_label() if voice.has_key() else 'browser only (no ELEVENLABS_API_KEY)'}")


@app.post("/api/upload")
async def upload(request: Request, session_id: str, filename: str = "document") -> dict:
    upload_limiter.check(client_key(request))
    sid = clean_session_id(session_id)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + 4096:
        raise HTTPException(413, TOO_BIG)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, TOO_BIG)
    try:
        ex = await run_in_threadpool(read_upload, filename, bytes(body))
    except DocumentError as exc:
        raise HTTPException(422, str(exc))
    return await run_in_threadpool(process_document, sid, ex)


@app.post("/api/sample")
def sample(body: SessionBody, request: Request) -> dict:
    upload_limiter.check(client_key(request))
    sid = clean_session_id(body.session_id)
    return process_document(sid, load_sample())


@app.post("/api/follow_links")
def follow_links(body: FollowBody, request: Request) -> dict:
    link_limiter.check(client_key(request))
    ws = get_workspace(clean_session_id(body.session_id))
    if ws is None:
        raise HTTPException(409, NEED_DOCUMENT)
    known = [l.url for l in ws.links]
    urls = [u for u in (body.urls if body.urls is not None else known) if u in known]
    if not urls:
        raise HTTPException(422, "There are no links to read in this document.")
    results = links.fetch_many(urls, OFFICIAL, max_links=10)
    have = {f.url for f in ws.linked}
    for r in results:
        if r.ok and r.fetched and r.fetched.url not in have:
            ws.linked.append(r.fetched)
            have.add(r.fetched.url)
    ws.linked_source = build_linked_source(ws.linked)
    return {
        "read": sum(1 for r in results if r.ok),
        "results": [
            {
                "url": r.url,
                "ok": r.ok,
                "title": r.title,
                "official": r.official,
                "reason": r.reason,
                "sections": len(r.fetched.pages) if r.fetched else 0,
            }
            for r in results
        ],
    }


@app.post("/api/add_link")
def add_link(body: LinkBody, request: Request) -> dict:
    link_limiter.check(client_key(request))
    ws = get_workspace(clean_session_id(body.session_id))
    if ws is None:
        raise HTTPException(409, NEED_DOCUMENT)
    try:
        fetched = links.fetch_public_page(links.prepare_link(body.url) or body.url, OFFICIAL)
    except links.LinkError as exc:
        raise HTTPException(422, str(exc))
    if fetched.url not in {f.url for f in ws.linked}:
        ws.linked.append(fetched)
    ws.linked_source = build_linked_source(ws.linked)
    return {"title": fetched.title, "url": fetched.url, "official": fetched.official, "sections": len(fetched.pages)}


@app.delete("/api/workspace")
def clear_workspace(session_id: str) -> dict:
    sid = clean_session_id(session_id)
    with SESSION_LOCK:
        SESSIONS.pop(sid, None)
    return {"ok": True}


@app.post("/api/ask")
def ask(body: AskBody, request: Request) -> dict:
    ask_limiter.check(client_key(request))
    ws = get_workspace(body.session_id)
    if ws is None:
        raise HTTPException(409, NEED_DOCUMENT)
    history = [t.model_dump() for t in body.history]

    try:
        first = ai.ask_source(ws.main, body.question, history)
        if first.covered:
            return {
                "covered": True,
                "origin": "main",
                "answer": first.answer,
                "source": source_card(ws.main, first.page, first.quote),
                "follow_ups": first.follow_ups or [],
            }
        if ws.linked_source is not None:
            second = ai.ask_source(ws.linked_source, body.question, history)
            if second.covered:
                return {
                    "covered": True,
                    "origin": "linked",
                    "answer": second.answer,
                    "source": source_card(ws.linked_source, second.page, second.quote),
                    "follow_ups": second.follow_ups or [],
                }
        if first.term_question and first.term:
            meaning = ai.explain_term(first.term)
            if meaning:
                return {
                    "covered": True,
                    "origin": "general",
                    "answer": f"{first.term.strip().capitalize()}: {meaning}",
                    "source": None,
                    "follow_ups": [],
                }
    except ai.AIUnavailable as exc:
        raise HTTPException(503, str(exc))

    return {
        "covered": False,
        "origin": "none",
        "answer": REFUSAL_LINKED if ws.linked_source is not None else REFUSAL,
        "source": None,
        "follow_ups": [],
    }


@app.post("/api/feedback")
def feedback(body: FeedbackBody) -> dict:
    append_line("feedback.jsonl", {"t": int(time.time()), **body.model_dump()})
    return {"ok": True}


# ---------------------------------------------------------------------- voice
# Optional ElevenLabs voice in and out. The key never leaves the server.

@app.post("/api/tts")
def tts(body: TtsBody, request: Request) -> Response:
    """Text in, MP3 out. Repeats are served from memory."""
    tts_limiter.check(client_key(request))
    try:
        audio = voice.tts(body.text, body.language)
    except voice.VoiceUnavailable as exc:
        raise HTTPException(503, str(exc))
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/stt")
async def stt(request: Request, filename: str = "speech.webm", language: str | None = None) -> dict:
    """A recording in (raw body, any common audio format), text out."""
    stt_limiter.check(client_key(request))
    if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
        raise HTTPException(422, "Unknown language code.")
    too_long = "That recording is too long. Try a shorter question."
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > voice.MAX_STT_BYTES + 4096:
        raise HTTPException(413, too_long)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > voice.MAX_STT_BYTES:
            raise HTTPException(413, too_long)
    if len(body) < 200:
        raise HTTPException(422, "I didn't catch that. Hold the button while you speak, then let go.")
    ctype = (request.headers.get("content-type") or "audio/webm").split(";")[0].strip() or "audio/webm"
    name = Path(filename or "speech.webm").name[:80] or "speech.webm"
    try:
        out = await run_in_threadpool(voice.stt, bytes(body), name, ctype, language)
    except voice.VoiceUnavailable as exc:
        raise HTTPException(503, str(exc))
    if not out.get("text"):
        raise HTTPException(422, "I couldn't hear any words. Try again, a little closer to the microphone.")
    return {"text": out["text"], "language_code": out.get("language_code")}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "microphone=(self)")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
