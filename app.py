from __future__ import annotations

import hashlib
import asyncio
import base64
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import File, UploadFile
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from ai_provider import answer as ai_answer

logger = logging.getLogger("study_buddy")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


configured_db_path = Path(os.getenv("STUDY_BUDDY_DB", "data/study_buddy.db"))
DB_PATH = configured_db_path if configured_db_path.is_absolute() else BASE_DIR / configured_db_path
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SESSION_DAYS = 14
MAX_MESSAGE_CHARS = 8_000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CONTEXT_CHARS = 24_000
RAG_TOP_K = 6
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMITS = {
    "auth": (10, RATE_LIMIT_WINDOW_SECONDS),
    "chat": (30, RATE_LIMIT_WINDOW_SECONDS),
    "upload": (6, RATE_LIMIT_WINDOW_SECONDS),
    "quiz": (10, RATE_LIMIT_WINDOW_SECONDS),
}
PHONE_VERIFICATION_TTL_MINUTES = 10
PHONE_VERIFICATION_DEV_MODE = os.getenv("PHONE_VERIFICATION_DEV_MODE", "1") == "1"
PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
PHONE_VERIFICATION_MAX_RESENDS = 3
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "dev").strip().lower()
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
if APP_ENV in {"production", "prod"} and (PHONE_VERIFICATION_DEV_MODE or SMS_PROVIDER == "dev"):
    raise RuntimeError("Production requires PHONE_VERIFICATION_DEV_MODE=0 and a real SMS_PROVIDER")
_rate_limit_hits: dict[tuple[str, str], list[float]] = {}
_embedding_model = None
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().-]{6,20}$")
AGE_RANGE_OPTIONS = ("Under 13", "13–15", "16–17", "18–24", "25+")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI-Powered Study Buddy", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL UNIQUE,
                identifier_type TEXT NOT NULL CHECK(identifier_type IN ('email', 'phone')),
                password_hash TEXT NOT NULL,
                full_name TEXT,
                age INTEGER,
                standard TEXT,
                age_range TEXT,
                phone TEXT,
                phone_verified INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS registration_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                resend_count INTEGER NOT NULL DEFAULT 0,
                last_sent_at TEXT,
                dev_code TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS study_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'note',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                score INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                prompt TEXT NOT NULL,
                answer TEXT NOT NULL,
                due_at TEXT NOT NULL,
                interval_days INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        for column, definition in (
            ("full_name", "TEXT"),
            ("age", "INTEGER"),
            ("standard", "TEXT"),
            ("age_range", "TEXT"),
            ("phone", "TEXT"),
            ("phone_verified", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        challenge_columns = {row[1] for row in connection.execute("PRAGMA table_info(registration_challenges)").fetchall()}
        for column, definition in (
            ("resend_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_sent_at", "TEXT"),
            ("dev_code", "TEXT"),
        ):
            if column not in challenge_columns:
                connection.execute(f"ALTER TABLE registration_challenges ADD COLUMN {column} {definition}")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL")
        connection.execute("DELETE FROM registration_challenges WHERE expires_at <= ?", (utc_now(),))


def normalize_identifier(value: str) -> tuple[str, str] | tuple[None, None]:
    value = value.strip()
    if EMAIL_RE.fullmatch(value):
        return value.lower(), "email"
    compact_phone = re.sub(r"[\s().-]", "", value)
    if PHONE_RE.fullmatch(value) and compact_phone.startswith("+"):
        return compact_phone, "phone"
    return None, None


def normalize_phone(value: str) -> str | None:
    value = value.strip()
    compact_phone = re.sub(r"[\s().-]", "", value)
    if PHONE_RE.fullmatch(value) and compact_phone.startswith("+"):
        return compact_phone
    return None


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}{'*' * max(4, len(phone) - 7)}{phone[-4:]}"


def generate_verification_code() -> str:
    configured_code = os.getenv("PHONE_VERIFICATION_CODE", "").strip()
    if configured_code.isdigit() and len(configured_code) == 6:
        return configured_code
    return f"{secrets.randbelow(1_000_000):06d}"


def deliver_verification_code(phone: str, code: str) -> bool:
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    twilio_api_key_sid = os.getenv("TWILIO_API_KEY_SID", "").strip()
    twilio_api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    twilio_from = os.getenv("TWILIO_FROM_PHONE", "").strip()
    twilio_credential_sid = twilio_api_key_sid or twilio_sid
    twilio_credential_secret = twilio_api_key_secret or twilio_token
    if SMS_PROVIDER == "twilio" or (twilio_sid and twilio_credential_secret and twilio_from):
        if not (twilio_sid and twilio_credential_sid and twilio_credential_secret and twilio_from):
            logger.error("Twilio phone verification selected but credentials are incomplete")
            return False
        endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
        body = urlencode({
            "To": phone,
            "From": twilio_from,
            "Body": f"Your Study Buddy verification code is {code}. It expires in {PHONE_VERIFICATION_TTL_MINUTES} minutes.",
        }).encode()
        auth = base64.b64encode(f"{twilio_credential_sid}:{twilio_credential_secret}".encode()).decode()
        request = UrlRequest(endpoint, data=body, headers={"Authorization": f"Basic {auth}"}, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300
        except Exception:
            logger.exception("SMS delivery failed for phone ending %s", phone[-4:])
            return False
    if SMS_PROVIDER == "dev" and PHONE_VERIFICATION_DEV_MODE:
        logger.info("Development phone verification code: %s", code)
        return True
    logger.error("No phone verification delivery provider is configured")
    return False


def create_registration_challenge(payload: dict) -> tuple[str, str] | None:
    token = secrets.token_urlsafe(32)
    code = generate_verification_code()
    if not deliver_verification_code(payload["phone"], code):
        return None
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=PHONE_VERIFICATION_TTL_MINUTES)
    code_hash = hashlib.sha256(f"{token}:{code}".encode()).hexdigest()
    with db() as connection:
        connection.execute(
            "INSERT INTO registration_challenges (token, phone, code_hash, payload_json, dev_code, last_sent_at, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                payload["phone"],
                code_hash,
                json.dumps(payload),
                code if PHONE_VERIFICATION_DEV_MODE and SMS_PROVIDER == "dev" else None,
                utc_now(),
                expires_at.isoformat(),
                utc_now(),
            ),
        )
    logger.info("Phone verification code generated for %s%s", payload["phone"][:4], "***")
    return token, code


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[-1]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def create_session(user_id: int) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (user_id, token_hash, expires.isoformat(), utc_now()),
        )
    return raw_token


def current_user(session_token: str | None) -> sqlite3.Row | None:
    if not session_token:
        return None
    token_hash = hashlib.sha256(session_token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute(
            """
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, utc_now()),
        ).fetchone()
    return row


def user_chat(user_id: int) -> list[sqlite3.Row]:
    with db() as connection:
        return connection.execute(
            "SELECT role, message, created_at FROM chat_messages WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()


def voice_history(user_id: int) -> list[sqlite3.Row]:
    with db() as connection:
        return connection.execute(
            "SELECT role, message, created_at FROM voice_messages WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()


def save_voice_turn(user_id: int, user_message: str, assistant_message: str) -> None:
    rows = []
    if user_message.strip():
        rows.append((user_id, "user", user_message.strip(), utc_now()))
    if assistant_message.strip():
        rows.append((user_id, "assistant", assistant_message.strip(), utc_now()))
    if rows:
        with db() as connection:
            connection.executemany(
                "INSERT INTO voice_messages (user_id, role, message, created_at) VALUES (?, ?, ?, ?)", rows
            )
        logger.info("Saved %s voice transcript rows for user %s", len(rows), user_id)


def document_chunks(filename: str, content: str) -> list[str]:
    pages = re.split(r"(?=\[Source:\s*[^,\]]+,\s*page\s+\d+\])", content)
    chunks: list[str] = []
    for page in pages:
        page = page.strip()
        if not page:
            continue
        paragraphs = [part.strip() for part in page.split("\n") if part.strip()]
        marker = paragraphs[0] if paragraphs and paragraphs[0].startswith("[Source:") else f"[Source: {filename}]"
        body = paragraphs[1:] if paragraphs and paragraphs[0].startswith("[Source:") else paragraphs
        current = marker
        for paragraph in body:
            if len(current) + len(paragraph) + 1 > 1_200 and current != marker:
                chunks.append(current)
                current = marker
            current += "\n" + paragraph
        if current != marker:
            chunks.append(current)
    return chunks


def semantic_scores(query: str, chunks: list[str]) -> list[float]:
    global _embedding_model
    if not chunks:
        return []
    try:
        if _embedding_model is None:
            from sentence_transformers import SentenceTransformer

            _embedding_model = SentenceTransformer(RAG_EMBEDDING_MODEL)
        vectors = _embedding_model.encode([query, *chunks], normalize_embeddings=True, show_progress_bar=False)
        query_vector = vectors[0]
        return [max(0.0, float(sum(a * b for a, b in zip(query_vector, vector)))) for vector in vectors[1:]]
    except Exception as error:
        logger.warning("Semantic RAG retrieval unavailable: %s", type(error).__name__)
        raise RuntimeError("rag_embedding_unavailable") from error


def context_for_user(user_id: int, query: str = "") -> str:
    with db() as connection:
        documents = connection.execute("SELECT filename, content FROM documents WHERE user_id = ?", (user_id,)).fetchall()
    terms = {term.lower() for term in re.findall(r"[a-zA-Z]{3,}", query)}
    chunks = [chunk for document in documents for chunk in document_chunks(document["filename"], document["content"])]
    if not chunks:
        return ""
    semantic = semantic_scores(query, chunks)
    lexical = [sum(term in chunk.lower() for term in terms) for chunk in chunks]
    max_lexical = max(lexical, default=1) or 1
    ranked = sorted(
        zip(chunks, lexical, semantic),
        key=lambda item: 0.45 * (item[1] / max_lexical) + 0.55 * item[2],
        reverse=True,
    )
    selected = [chunk for chunk, _, _ in ranked[:RAG_TOP_K]]
    total_chars = 0
    bounded: list[str] = []
    for chunk in selected:
        if total_chars + len(chunk) > MAX_CONTEXT_CHARS:
            break
        bounded.append(chunk)
        total_chars += len(chunk)
    return "\n".join(bounded)


def redirect_with_session(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("study_session", token, max_age=SESSION_DAYS * 86400, httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax")
    return response


def api_auth_error() -> None:
    raise HTTPException(status_code=401, detail="authentication_required")


def enforce_rate_limit(request: Request, bucket: str, identity: str = "anonymous") -> None:
    limit, window = RATE_LIMITS[bucket]
    key = (bucket, f"{request.client.host if request.client else 'unknown'}:{identity}")
    now = time.monotonic()
    hits = [stamp for stamp in _rate_limit_hits.get(key, []) if now - stamp < window]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": str(window)})
    hits.append(now)
    _rate_limit_hits[key] = hits


def validate_csrf(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.url.path in {"/login", "/register", "/verify-phone", "/verify-phone/resend", "/logout"}:
        return
    cookie_token = request.cookies.get("csrf_token")
    supplied = request.headers.get("x-csrf-token") or request.headers.get("x-csrftoken")
    if not cookie_token or not supplied or not hmac.compare_digest(cookie_token, supplied):
        raise HTTPException(status_code=403, detail="csrf_validation_failed")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    validate_csrf(request)
    response = await call_next(request)
    if request.url.path == "/general" and response.headers.get("content-type", "").startswith("text/html"):
        chunks = [chunk async for chunk in response.body_iterator]
        body = b"".join(chunks).decode("utf-8")
        body = body.replace("</head>", '<script src="/static/js/escalation.js"></script></head>')
        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        response = HTMLResponse(content=body, status_code=response.status_code, headers=headers)
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", secrets.token_urlsafe(32), max_age=SESSION_DAYS * 86400, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "study-buddy"}


@app.websocket("/ws/live")
async def live_teacher(websocket: WebSocket):
    await websocket.accept()
    user = current_user(websocket.cookies.get("study_session"))
    if not user:
        await websocket.send_json({"type": "error", "code": "auth_required", "message": "Your study session expired. Sign in again to use the voice teacher."})
        await websocket.close()
        return
    if not os.getenv("GEMINI_API_KEY"):
        await websocket.send_json({"type": "error", "code": "missing_key", "message": "GEMINI_API_KEY is not configured on the server."})
        await websocket.close()
        return
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": "You are a patient personal teacher. Start speaking quickly with a concise answer of no more than 3 short sentences, then ask one guiding question. Explain step by step and simplify when the student says they are confused. Study context: " + context_for_user(user["id"]),
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {"automatic_activity_detection": {"silence_duration_ms": 500}},
        }
        async with client.aio.live.connect(model=os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"), config=config) as session:
            pending_user = ""
            pending_assistant = ""

            async def receive_browser():
                while True:
                    try:
                        message = await websocket.receive_json()
                    except WebSocketDisconnect:
                        save_voice_turn(user["id"], pending_user, pending_assistant)
                        raise
                    if message.get("type") == "audio":
                        await session.send_realtime_input(audio=types.Blob(data=base64.b64decode(message["data"]), mime_type="audio/pcm;rate=16000"))
                    elif message.get("type") == "text":
                        await session.send_realtime_input(text=message.get("text", ""))

            async def send_browser():
                nonlocal pending_user, pending_assistant
                while True:
                    async for response in session.receive():
                        content = response.server_content
                        if content and content.model_turn:
                            for part in content.model_turn.parts:
                                if part.inline_data:
                                    await websocket.send_json({"type": "audio", "data": base64.b64encode(part.inline_data.data).decode(), "mime_type": part.inline_data.mime_type or "audio/pcm;rate=24000"})
                        if content and content.input_transcription:
                            text = content.input_transcription.text or ""
                            pending_user += text
                            await websocket.send_json({"type": "transcript", "role": "user", "text": text})
                        if content and content.output_transcription:
                            text = content.output_transcription.text or ""
                            pending_assistant += text
                            await websocket.send_json({"type": "transcript", "role": "assistant", "text": text})
                        if content and getattr(content, "turn_complete", False):
                            save_voice_turn(user["id"], pending_user, pending_assistant)
                            pending_user = ""
                            pending_assistant = ""
                            await websocket.send_json({"type": "turn_complete"})

            await asyncio.gather(receive_browser(), send_browser())
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception as error:
        logger.exception("Gemini Live session failed: %s", type(error).__name__)
        try:
            await websocket.send_json({"type": "error", "code": "live_error", "message": "Live teacher paused. The connection will try again automatically."})
        except Exception:
            pass


@app.get("/", response_class=HTMLResponse)
def home(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "auth/login_slow.html", {"error": None, "next": "/dashboard"})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "auth/register.html", {"error": None})


@app.post("/register")
def register(
    request: Request,
    identifier: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    full_name: str = Form(...),
    age_range: str = Form(""),
    age: str = Form(""),
    standard: str = Form(...),
    phone: str = Form(...),
):
    enforce_rate_limit(request, "auth")
    normalized, identifier_type = normalize_identifier(identifier)
    if not normalized:
        return RedirectResponse("/register?error=Use a valid email or international phone number", status_code=303)
    full_name = " ".join(full_name.split())
    standard = " ".join(standard.split())
    normalized_phone = normalize_phone(phone)
    if len(full_name) < 2 or len(full_name) > 80:
        return RedirectResponse("/register?error=Enter your full name", status_code=303)
    age_range = " ".join(age_range.split())
    if not age_range and age:
        try:
            legacy_age = int(age)
            age_range = "Under 13" if legacy_age < 13 else "13–15" if legacy_age <= 15 else "16–17" if legacy_age <= 17 else "18–24" if legacy_age <= 24 else "25+"
        except (TypeError, ValueError):
            age_range = ""
    if age_range not in AGE_RANGE_OPTIONS:
        return RedirectResponse("/register?error=Choose your age range", status_code=303)
    if not standard or len(standard) > 80:
        return RedirectResponse("/register?error=Enter your standard or grade", status_code=303)
    if not normalized_phone:
        return RedirectResponse("/register?error=Use a valid international phone number for verification", status_code=303)
    enforce_rate_limit(request, "auth", identity=f"registration:{normalized_phone}")
    if len(password) < 8:
        return RedirectResponse("/register?error=Password must be at least 8 characters", status_code=303)
    if password != confirm_password:
        return RedirectResponse("/register?error=Passwords do not match", status_code=303)
    with db() as connection:
        connection.execute("DELETE FROM registration_challenges WHERE expires_at <= ?", (utc_now(),))
        duplicate = connection.execute(
            "SELECT 1 FROM users WHERE identifier = ? OR phone = ?", (normalized, normalized_phone)
        ).fetchone()
    if duplicate:
        return RedirectResponse("/register?error=That email or phone number is already registered", status_code=303)
    payload = {
        "identifier": normalized,
        "identifier_type": identifier_type,
        "password_hash": hash_password(password),
        "full_name": full_name,
        "age_range": age_range,
        "standard": standard,
        "phone": normalized_phone,
    }
    challenge = create_registration_challenge(payload)
    if not challenge:
        return RedirectResponse(
            "/register?error=We could not send a verification code. Configure phone delivery and try again",
            status_code=303,
        )
    token, _code = challenge
    return RedirectResponse(f"/verify-phone?token={quote_plus(token)}", status_code=303)


@app.get("/verify-phone", response_class=HTMLResponse)
def verify_phone_page(request: Request, token: str = "", error: str | None = None, resent: int = 0):
    dev_code = None
    masked = None
    cooldown_seconds = 0
    mode = "registration"
    if token and PHONE_VERIFICATION_DEV_MODE and SMS_PROVIDER == "dev":
        with db() as connection:
            challenge = connection.execute(
                "SELECT dev_code, phone, last_sent_at, payload_json FROM registration_challenges WHERE token = ?", (token,)
            ).fetchone()
        if challenge:
            dev_code = challenge["dev_code"]
            masked = mask_phone(challenge["phone"])
            mode = "phone_change" if json.loads(challenge["payload_json"]).get("kind") == "phone_change" else "registration"
            if challenge["last_sent_at"]:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(challenge["last_sent_at"])).total_seconds()
                cooldown_seconds = max(0, int(PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed))
    elif token:
        with db() as connection:
            challenge = connection.execute(
                "SELECT phone, last_sent_at FROM registration_challenges WHERE token = ?", (token,)
            ).fetchone()
        if challenge:
            masked = mask_phone(challenge["phone"])
            mode = "phone_change" if json.loads(challenge["payload_json"]).get("kind") == "phone_change" else "registration"
            if challenge["last_sent_at"]:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(challenge["last_sent_at"])).total_seconds()
                cooldown_seconds = max(0, int(PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed))
    return templates.TemplateResponse(
        request,
        "auth/verify_phone.html",
        {"error": error, "token": token, "dev_code": dev_code, "resent": bool(resent), "masked_phone": masked, "cooldown_seconds": cooldown_seconds, "mode": mode},
    )


@app.post("/verify-phone/resend")
def resend_phone_verification(request: Request, token: str = Form(...)):
    with db() as connection:
        connection.execute("DELETE FROM registration_challenges WHERE expires_at <= ?", (utc_now(),))
    enforce_rate_limit(request, "auth", identity=f"verify:{token}")
    with db() as connection:
        challenge = connection.execute(
            "SELECT * FROM registration_challenges WHERE token = ?", (token,)
        ).fetchone()
    if not challenge:
        return RedirectResponse("/register?error=That verification request is no longer available", status_code=303)
    now = datetime.now(timezone.utc)
    if datetime.fromisoformat(challenge["expires_at"]) <= now:
        return RedirectResponse("/register?error=That verification code expired. Please register again", status_code=303)
    if challenge["resend_count"] >= PHONE_VERIFICATION_MAX_RESENDS:
        return RedirectResponse(
            f"/verify-phone?token={quote_plus(token)}&error={quote_plus('Resend limit reached. Please register again')}",
            status_code=303,
        )
    if challenge["last_sent_at"]:
        elapsed = (now - datetime.fromisoformat(challenge["last_sent_at"])).total_seconds()
        if elapsed < PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS:
            wait_seconds = max(1, int(PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed))
            message = f"Please wait {wait_seconds} seconds before requesting another code"
            return RedirectResponse(
                f"/verify-phone?token={quote_plus(token)}&error={quote_plus(message)}", status_code=303
            )
    code = generate_verification_code()
    if not deliver_verification_code(challenge["phone"], code):
        return RedirectResponse(
            f"/verify-phone?token={quote_plus(token)}&error={quote_plus('We could not send a new code')}",
            status_code=303,
        )
    expires_at = now + timedelta(minutes=PHONE_VERIFICATION_TTL_MINUTES)
    with db() as connection:
        connection.execute(
            "UPDATE registration_challenges SET code_hash = ?, dev_code = ?, resend_count = resend_count + 1, last_sent_at = ?, expires_at = ? WHERE token = ?",
            (
                hashlib.sha256(f"{token}:{code}".encode()).hexdigest(),
                code if PHONE_VERIFICATION_DEV_MODE and SMS_PROVIDER == "dev" else None,
                now.isoformat(),
                expires_at.isoformat(),
                token,
            ),
        )
    return RedirectResponse(f"/verify-phone?token={quote_plus(token)}&resent=1", status_code=303)


@app.post("/verify-phone")
def verify_phone(request: Request, token: str = Form(...), code: str = Form(...)):
    with db() as connection:
        connection.execute("DELETE FROM registration_challenges WHERE expires_at <= ?", (utc_now(),))
        challenge = connection.execute(
            "SELECT * FROM registration_challenges WHERE token = ?", (token,)
        ).fetchone()
    if challenge:
        enforce_rate_limit(request, "auth", identity=f"verify:{challenge['phone']}")
    else:
        enforce_rate_limit(request, "auth", identity=f"verify:{token}")
    if not challenge:
        return RedirectResponse("/register?error=That verification request is no longer available", status_code=303)
    if datetime.fromisoformat(challenge["expires_at"]) <= datetime.now(timezone.utc):
        with db() as connection:
            connection.execute("DELETE FROM registration_challenges WHERE token = ?", (token,))
        return RedirectResponse("/register?error=That verification code expired. Please register again", status_code=303)
    if challenge["attempts"] >= 5:
        return RedirectResponse("/register?error=Too many verification attempts. Please register again", status_code=303)
    expected_hash = hashlib.sha256(f"{token}:{code.strip()}".encode()).hexdigest()
    if not hmac.compare_digest(expected_hash, challenge["code_hash"]):
        with db() as connection:
            connection.execute("UPDATE registration_challenges SET attempts = attempts + 1 WHERE token = ?", (token,))
        return RedirectResponse(
            f"/verify-phone?token={quote_plus(token)}&error=That verification code is incorrect",
            status_code=303,
        )
    payload = json.loads(challenge["payload_json"])
    if payload.get("kind") == "phone_change":
        with db() as connection:
            connection.execute(
                "UPDATE users SET phone = ?, phone_verified = 1 WHERE id = ?",
                (payload["phone"], payload["user_id"]),
            )
            connection.execute("DELETE FROM registration_challenges WHERE token = ?", (token,))
        return RedirectResponse("/profile?message=Phone number verified and updated", status_code=303)
    try:
        with db() as connection:
            cursor = connection.execute(
                "INSERT INTO users (identifier, identifier_type, password_hash, full_name, age_range, standard, phone, phone_verified, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)",
                (
                    payload["identifier"],
                    payload["identifier_type"],
                    payload["password_hash"],
                    payload["full_name"],
                    payload["age_range"],
                    payload["standard"],
                    payload["phone"],
                    utc_now(),
                ),
            )
            user_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO study_items (user_id, title, kind, created_at) VALUES (?, ?, ?, ?)",
                (user_id, "Welcome to your study space", "starter", utc_now()),
            )
            connection.execute("DELETE FROM registration_challenges WHERE token = ?", (token,))
    except sqlite3.IntegrityError:
        return RedirectResponse("/register?error=That email or phone number is already registered", status_code=303)
    return redirect_with_session("/dashboard", create_session(user_id))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None, next: str = "/dashboard"):
    return templates.TemplateResponse(request, "auth/login_slow.html", {"error": error, "next": next})


@app.post("/login")
def login(request: Request, identifier: str = Form(...), password: str = Form(...), next: str = Form("/dashboard")):
    enforce_rate_limit(request, "auth")
    normalized, _ = normalize_identifier(identifier)
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE identifier = ?", (normalized or identifier.strip().lower(),)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse(f"/login?error=Invalid+login+details&next={next}", status_code=303)
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    return redirect_with_session(safe_next, create_session(user["id"]))


@app.post("/logout")
def logout(study_session: str | None = Cookie(default=None)):
    if study_session:
        token_hash = hashlib.sha256(study_session.encode()).hexdigest()
        with db() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("study_session")
    return response


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, study_session: str | None = Cookie(default=None), message: str | None = None):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/profile", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth/profile.html",
        {"user": user, "message": message, "csrf_token": request.cookies.get("csrf_token", "")},
    )


@app.post("/profile")
def update_profile(
    request: Request,
    full_name: str = Form(...),
    age_range: str = Form(...),
    standard: str = Form(...),
    study_session: str | None = Cookie(default=None),
):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/profile", status_code=303)
    full_name = " ".join(full_name.split())
    standard = " ".join(standard.split())
    if len(full_name) < 2 or len(full_name) > 80 or age_range not in AGE_RANGE_OPTIONS or not standard or len(standard) > 80:
        return RedirectResponse("/profile?message=Please complete all profile fields", status_code=303)
    with db() as connection:
        connection.execute(
            "UPDATE users SET full_name = ?, age_range = ?, standard = ? WHERE id = ?",
            (full_name, age_range, standard, user["id"]),
        )
    return RedirectResponse("/profile?message=Profile saved", status_code=303)


@app.post("/profile/phone")
def change_phone(request: Request, phone: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/profile", status_code=303)
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        return RedirectResponse("/profile?message=Use a valid international phone number", status_code=303)
    enforce_rate_limit(request, "auth", identity=f"phone-change:{normalized_phone}")
    with db() as connection:
        duplicate = connection.execute(
            "SELECT 1 FROM users WHERE phone = ? AND id != ?", (normalized_phone, user["id"])
        ).fetchone()
    if duplicate:
        return RedirectResponse("/profile?message=That phone number is already registered", status_code=303)
    challenge = create_registration_challenge({"kind": "phone_change", "user_id": user["id"], "phone": normalized_phone})
    if not challenge:
        return RedirectResponse("/profile?message=We could not send a verification code", status_code=303)
    token, _code = challenge
    return RedirectResponse(f"/verify-phone?token={quote_plus(token)}", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/dashboard", status_code=303)
    with db() as connection:
        items = connection.execute(
            "SELECT title, kind, created_at FROM study_items WHERE user_id = ? ORDER BY id DESC", (user["id"],)
        ).fetchall()
        stats = connection.execute(
            "SELECT (SELECT COUNT(*) FROM chat_messages WHERE user_id = ?) messages, (SELECT COUNT(*) FROM documents WHERE user_id = ?) documents, (SELECT COUNT(*) FROM quizzes WHERE user_id = ?) quizzes, (SELECT COALESCE(AVG(score * 100.0 / NULLIF(json_array_length(questions_json), 0)), 0) FROM quizzes WHERE user_id = ? AND score IS NOT NULL) accuracy",
            (user["id"], user["id"], user["id"], user["id"]),
        ).fetchone()
        quiz_history = connection.execute(
            "SELECT id, title, score, json_array_length(questions_json) question_count, created_at FROM quizzes WHERE user_id = ? ORDER BY id DESC LIMIT 6",
            (user["id"],),
        ).fetchall()
    return templates.TemplateResponse(
        request, "pages/dashboard4.html", {"user": user, "items": items, "messages": user_chat(user["id"]), "stats": stats, "quiz_history": quiz_history, "provider_ready": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY"))}
    )


@app.get("/voice", response_class=HTMLResponse)
def voice_page(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/voice", status_code=303)
    return templates.TemplateResponse(
        request, "pages/dashboard3.html", {"user": user, "voice_messages": voice_history(user["id"]), "voice_reason": request.query_params.get("reason")}
    )


@app.get("/api/voice/history")
def get_voice_history(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    return {"messages": [dict(message) for message in voice_history(user["id"])]}


@app.delete("/api/voice/history")
def clear_voice_history(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        connection.execute("DELETE FROM voice_messages WHERE user_id = ?", (user["id"],))
    logger.info("Cleared voice transcript for user %s", user["id"])
    return {"cleared": True}


@app.get("/api/voice/export")
def export_voice_history(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    lines = [f"Study Buddy voice teacher transcript for {user['identifier']}", ""]
    for message in voice_history(user["id"]):
        lines.append(f"{message['role'].upper()}: {message['message']}")
        lines.append("")
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": "attachment; filename=study-buddy-voice-transcript.txt"},
    )


@app.get("/general", response_class=HTMLResponse)
def general_page(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/general", status_code=303)
    return templates.TemplateResponse(
        request, "pages/general_chat.html", {"user": user, "messages": user_chat(user["id"])}
    )


@app.get("/rag", response_class=HTMLResponse)
def rag_page(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+log+in+first&next=/rag", status_code=303)
    with db() as connection:
        documents = connection.execute(
            "SELECT id, filename, created_at FROM documents WHERE user_id = ? ORDER BY id DESC", (user["id"],)
        ).fetchall()
    return templates.TemplateResponse(
        request, "pages/rag_workspace.html", {"user": user, "documents": documents, "messages": user_chat(user["id"])}
    )


@app.get("/api/chat")
def get_chat(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    return {"messages": [dict(message) for message in user_chat(user["id"])]}


@app.post("/api/chat")
def send_chat(request: Request, message: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="message_required")
    if len(clean_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="message_too_long")
    # Plain chatbot path: no uploaded-document context is injected.
    response_text = ai_answer(clean_message)
    with db() as connection:
        connection.executemany(
            "INSERT INTO chat_messages (user_id, role, message, created_at) VALUES (?, ?, ?, ?)",
            [(user["id"], "user", clean_message, utc_now()), (user["id"], "assistant", response_text, utc_now())],
        )
    return {"messages": [dict(message) for message in user_chat(user["id"])]}


@app.post("/api/rag/chat")
def send_rag_chat(request: Request, message: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="message_required")
    if len(clean_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="message_too_long")
    try:
        retrieved_context = context_for_user(user["id"], clean_message)
    except RuntimeError:
        raise HTTPException(status_code=503, detail="rag_embedding_unavailable")
    response_text = ai_answer(clean_message, retrieved_context)
    with db() as connection:
        connection.executemany(
            "INSERT INTO chat_messages (user_id, role, message, created_at) VALUES (?, ?, ?, ?)",
            [(user["id"], "user", clean_message, utc_now()), (user["id"], "assistant", response_text, utc_now())],
        )
    return {"messages": [dict(message) for message in user_chat(user["id"])]}


@app.post("/api/documents")
async def upload_document(request: Request, file: UploadFile = File(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "upload", str(user["id"]))
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return {"error": "pdf_only"}
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="pdf_too_large")
    if not raw.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="invalid_pdf")
    text = ""
    try:
        import fitz
        document = fitz.open(stream=raw, filetype="pdf")
        text = "\n\n".join(
            f"[Source: {file.filename}, page {page_number}]\n{page.get_text()}"
            for page_number, page in enumerate(document, start=1)
        )
    except Exception:
        text = ""
    if not text.strip():
        return {"error": "could_not_extract_text"}
    with db() as connection:
        cursor = connection.execute("INSERT INTO documents (user_id, filename, content, created_at) VALUES (?, ?, ?, ?)", (user["id"], file.filename, text[:2_000_000], utc_now()))
    return {"id": cursor.lastrowid, "filename": file.filename, "characters": len(text)}


@app.get("/api/documents")
def list_documents(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        rows = connection.execute("SELECT id, filename, created_at FROM documents WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    return {"documents": [dict(row) for row in rows]}


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        cursor = connection.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"]))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"deleted": True, "document_id": document_id}


@app.post("/api/quiz/generate")
def generate_quiz(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "quiz", str(user["id"]))
    try:
        context = context_for_user(user["id"])
    except RuntimeError:
        raise HTTPException(status_code=503, detail="rag_embedding_unavailable")
    if not context:
        raise HTTPException(status_code=400, detail="upload_pdf_first")
    prompt = "Create 3 multiple-choice study questions from the supplied material. Return JSON array with question, options (4 strings), answer (0-3), explanation. Material:\n" + context
    raw = ai_answer("Create the quiz as JSON only.", context=context + "\n\n" + prompt)
    questions = []
    try:
        candidate = raw[raw.find("["):raw.rfind("]") + 1]
        questions = json.loads(candidate)
        if not isinstance(questions, list) or not questions or len(questions) > 10:
            raise ValueError("invalid quiz list")
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("question"), str) or not isinstance(question.get("options"), list) or len(question["options"]) != 4 or int(question.get("answer", -1)) not in range(4):
                raise ValueError("invalid quiz question")
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="quiz_generation_failed")
    with db() as connection:
        cursor = connection.execute("INSERT INTO quizzes (user_id, title, questions_json, created_at) VALUES (?, ?, ?, ?)", (user["id"], "Quick review", json.dumps(questions), utc_now()))
    return {"quiz_id": cursor.lastrowid, "title": "Quick review", "questions": questions}


@app.post("/api/quiz/{quiz_id}/submit")
def submit_quiz(quiz_id: int, answers: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        quiz = connection.execute("SELECT * FROM quizzes WHERE id = ? AND user_id = ?", (quiz_id, user["id"])).fetchone()
        if not quiz:
            return {"error": "not_found"}
        try:
            questions = json.loads(quiz["questions_json"])
            submitted = json.loads(answers)
            if not isinstance(submitted, list) or len(submitted) != len(questions):
                raise ValueError("answer count mismatch")
            score = sum(int(item.get("answer", -1)) == int(submitted[index]) for index, item in enumerate(questions))
        except (ValueError, TypeError, json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=400, detail="invalid_answers")
        connection.execute("UPDATE quizzes SET score = ? WHERE id = ?", (score, quiz_id))
    return {"score": score, "total": len(questions)}


@app.get("/api/reviews")
def reviews(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        rows = connection.execute("SELECT id, prompt, answer, due_at, interval_days FROM review_cards WHERE user_id = ? ORDER BY due_at", (user["id"],)).fetchall()
    return {"reviews": [dict(row) for row in rows]}


@app.post("/api/reviews")
def create_review(prompt: str = Form(...), answer: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    if not prompt.strip() or not answer.strip() or len(prompt) > MAX_MESSAGE_CHARS or len(answer) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="review_content_invalid")
    due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO review_cards (user_id, prompt, answer, due_at, interval_days, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], prompt.strip(), answer.strip(), due_at, 1, utc_now()),
        )
    return {"id": cursor.lastrowid, "due_at": due_at}


@app.get("/api/analytics")
def analytics(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        totals = connection.execute(
            "SELECT (SELECT COUNT(*) FROM documents WHERE user_id = ?) documents, (SELECT COUNT(*) FROM chat_messages WHERE user_id = ?) messages, (SELECT COUNT(*) FROM quizzes WHERE user_id = ?) quizzes, (SELECT COALESCE(AVG(score * 1.0 / NULLIF(json_array_length(questions_json), 0)), 0) FROM quizzes WHERE user_id = ?) average_quiz_score",
            (user["id"], user["id"], user["id"], user["id"]),
        ).fetchone()
    return dict(totals)


@app.get("/api/export/chat")
def export_chat(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    lines = [f"Study Buddy chat export for {user['identifier']}", ""]
    for message in user_chat(user["id"]):
        lines.append(f"{message['role'].upper()}: {message['message']}")
    return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": "attachment; filename=study-buddy-chat.txt"})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user or not user["is_admin"]:
        return RedirectResponse("/dashboard", status_code=303)
    with db() as connection:
        users = connection.execute("SELECT id, identifier, identifier_type, created_at FROM users ORDER BY id DESC").fetchall()
        counts = connection.execute("SELECT (SELECT COUNT(*) FROM documents) documents, (SELECT COUNT(*) FROM chat_messages) messages, (SELECT COUNT(*) FROM quizzes) quizzes").fetchone()
    return templates.TemplateResponse(request, "admin/admin.html", {"user": user, "users": users, "counts": counts})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
