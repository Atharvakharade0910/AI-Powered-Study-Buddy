from __future__ import annotations

import hashlib
import asyncio
import base64
import binascii
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import File, UploadFile
from dotenv import load_dotenv
from database import PostgresConnection
from storage import delete_pdf, put_pdf
from malware_scanner import MalwareScanError, scan_bytes

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from ai_provider import answer as ai_answer, stream_answer as ai_stream_answer

logger = logging.getLogger("study_buddy")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")


configured_db_path = Path(os.getenv("STUDY_BUDDY_DB", "data/study_buddy.db"))
DB_PATH = configured_db_path if configured_db_path.is_absolute() else BASE_DIR / configured_db_path
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SESSION_DAYS = 14
MAX_MESSAGE_CHARS = 8_000
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
try:
    MAX_UPLOAD_PAGES = max(1, int(os.getenv("MAX_UPLOAD_PAGES", "200")))
except ValueError:
    MAX_UPLOAD_PAGES = 200
MAX_CONTEXT_CHARS = 24_000
MAX_DOCUMENTS_PER_USER = 50
MAX_DOCUMENT_STORAGE_CHARS = 20_000_000
RAG_TOP_K = 6
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMITS = {
    "auth": (10, RATE_LIMIT_WINDOW_SECONDS),
    "chat": (30, RATE_LIMIT_WINDOW_SECONDS),
    "upload": (6, RATE_LIMIT_WINDOW_SECONDS),
    "quiz": (10, RATE_LIMIT_WINDOW_SECONDS),
    "review": (30, RATE_LIMIT_WINDOW_SECONDS),
}
MAX_CHAT_HISTORY = 100
MAX_WS_AUDIO_BYTES = 96 * 1024
MAX_WS_TEXT_CHARS = 8_000
MAX_WS_MESSAGES_PER_MINUTE = 1_200
MAX_LIVE_CONNECTIONS_PER_USER = 1
PHONE_VERIFICATION_TTL_MINUTES = 10
PHONE_VERIFICATION_DEV_MODE = os.getenv("PHONE_VERIFICATION_DEV_MODE", "1") == "1"
PHONE_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
PHONE_VERIFICATION_MAX_RESENDS = 3
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "dev").strip().lower()
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
ALLOWED_ORIGINS = {
    origin.rstrip("/")
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
if APP_ENV in {"production", "prod"} and not DATABASE_URL.startswith(("postgres://", "postgresql://")):
    raise RuntimeError("Production requires DATABASE_URL to point to PostgreSQL")
if APP_ENV in {"production", "prod"} and not os.getenv("REDIS_URL", "").strip():
    raise RuntimeError("Production requires REDIS_URL for shared rate limits and live-session coordination")
if APP_ENV in {"production", "prod"} and (PHONE_VERIFICATION_DEV_MODE or SMS_PROVIDER == "dev"):
    raise RuntimeError("Production requires PHONE_VERIFICATION_DEV_MODE=0 and a real SMS_PROVIDER")
if APP_ENV in {"production", "prod"} and os.getenv("PHONE_VERIFICATION_CODE", "").strip():
    raise RuntimeError("Production must not use a fixed phone verification code")
if APP_ENV in {"production", "prod"} and os.getenv("COOKIE_SECURE", "0") != "1":
    raise RuntimeError("Production requires COOKIE_SECURE=1")
_rate_limit_hits: dict[tuple[str, str], list[float]] = {}
_live_connections: dict[int, int] = {}
_embedding_model = None
_redis_client = None
_document_executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("DOCUMENT_WORKERS", "2"))))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().-]{6,20}$")
AGE_RANGE_OPTIONS = ("Under 13", "13–15", "16–17", "18–24", "25+")
STANDARD_OPTIONS = tuple(f"Standard {number}" for number in range(1, 10))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI-Powered Study Buddy", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> Any:
    if DATABASE_URL.startswith(("postgres://", "postgresql://")):
        return PostgresConnection(DATABASE_URL)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
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
                onboarding_completed INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('general', 'document', 'voice')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
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
                storage_key TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT 'Document study',
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
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
                repetitions INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                topic TEXT NOT NULL DEFAULT 'Document study',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_documents (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, document_id)
            );
            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                answer_hash TEXT NOT NULL,
                score INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                review_cards_created INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE (quiz_id, answer_hash)
            );
            CREATE TABLE IF NOT EXISTS learning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                score REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learner_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'learner',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, memory_key)
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
            ("onboarding_completed", "INTEGER NOT NULL DEFAULT 0"),
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
        chat_columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_messages)").fetchall()}
        if "conversation_id" not in chat_columns:
            connection.execute("ALTER TABLE chat_messages ADD COLUMN conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id, updated_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_learning_events_user_topic ON learning_events(user_id, topic, created_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_learner_memories_user ON learner_memories(user_id, updated_at DESC)")
        quiz_columns = {row[1] for row in connection.execute("PRAGMA table_info(quizzes)").fetchall()}
        for column, definition in (("topic", "TEXT NOT NULL DEFAULT 'Document study'"), ("document_id", "INTEGER")):
            if column not in quiz_columns:
                connection.execute(f"ALTER TABLE quizzes ADD COLUMN {column} {definition}")
        review_columns = {row[1] for row in connection.execute("PRAGMA table_info(review_cards)").fetchall()}
        for column, definition in (("repetitions", "INTEGER NOT NULL DEFAULT 0"), ("completed_at", "TEXT"), ("topic", "TEXT NOT NULL DEFAULT 'Document study'")):
            if column not in review_columns:
                connection.execute(f"ALTER TABLE review_cards ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_conversation_documents_document ON conversation_documents(document_id)")
        document_columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)").fetchall()}
        if "storage_key" not in document_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN storage_key TEXT")
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


def normalize_standard(value: str) -> str | None:
    match = re.fullmatch(r"(?:standard|std|grade)?\s*([1-9])", value.strip(), re.IGNORECASE)
    return f"Standard {match.group(1)}" if match else None


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


def get_or_create_conversation(user_id: int, conversation_id: int | None = None, mode: str = "general") -> sqlite3.Row:
    with db() as connection:
        row = None
        if conversation_id:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ? AND mode = ?",
                (conversation_id, user_id, mode),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="conversation_not_found")
        if not row:
            row = connection.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND mode = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
                (user_id, mode),
            ).fetchone()
        if row:
            return row
        now = utc_now()
        cursor = connection.execute(
            "INSERT INTO conversations (user_id, title, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, "General study" if mode == "general" else "New document study", mode, now, now),
        )
        return connection.execute("SELECT * FROM conversations WHERE id = ?", (cursor.lastrowid,)).fetchone()


def user_conversations(user_id: int, mode: str | None = None) -> list[sqlite3.Row]:
    with db() as connection:
        if mode:
            return connection.execute(
                "SELECT * FROM conversations WHERE user_id = ? AND mode = ? ORDER BY updated_at DESC, id DESC",
                (user_id, mode),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC", (user_id,)
        ).fetchall()


def user_conversation(user_id: int, conversation_id: int) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
        ).fetchone()


def attach_legacy_documents(user_id: int, conversation_id: int) -> None:
    with db() as connection:
        attached = connection.execute(
            "SELECT 1 FROM conversation_documents WHERE conversation_id = ? LIMIT 1", (conversation_id,)
        ).fetchone()
        if not attached:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_documents (conversation_id, document_id, created_at) SELECT ?, id, ? FROM documents WHERE user_id = ?",
                (conversation_id, utc_now(), user_id),
            )


def save_chat_turn(user_id: int, user_message: str, assistant_message: str, conversation_id: int | None = None, mode: str = "general") -> int:
    conversation = get_or_create_conversation(user_id, conversation_id, mode)
    capture_explicit_memory(user_id, user_message, f"{mode}_conversation")
    title = conversation["title"]
    if title in {"General study", "New document study"}:
        title = user_message.strip().replace("\n", " ")[:60] or title
    with db() as connection:
        connection.executemany(
            "INSERT INTO chat_messages (user_id, conversation_id, role, message, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (user_id, conversation["id"], "user", user_message, utc_now()),
                (user_id, conversation["id"], "assistant", assistant_message, utc_now()),
            ],
        )
        connection.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (title, utc_now(), conversation["id"], user_id),
        )
    return int(conversation["id"])


def record_learning_event(user_id: int, event_type: str, topic: str, score: float | None = None, metadata: dict | None = None) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO learning_events (user_id, event_type, topic, score, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, event_type, topic[:120], score, json.dumps(metadata or {}), utc_now()),
        )


def learner_memories(user_id: int, limit: int = 30) -> list[sqlite3.Row]:
    with db() as connection:
        return connection.execute(
            "SELECT memory_key, memory_value, source, created_at, updated_at FROM learner_memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 50))),
        ).fetchall()


def learner_memory_context(user_id: int, max_chars: int = 5_000) -> str:
    rows = learner_memories(user_id)
    return "\n".join(f"{row['memory_key']}: {row['memory_value']}" for row in reversed(rows))[-max_chars:]


def set_learner_memory(user_id: int, key: str, value: str, source: str = "learner") -> None:
    now = utc_now()
    with db() as connection:
        connection.execute(
            "INSERT INTO learner_memories (user_id, memory_key, memory_value, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, memory_key) DO UPDATE SET memory_value = excluded.memory_value, source = excluded.source, updated_at = excluded.updated_at",
            (user_id, key[:80], value[:500], source[:40], now, now),
        )


def capture_explicit_memory(user_id: int, text: str, source: str = "conversation") -> None:
    """Capture only clearly stated goals/preferences, never arbitrary messages."""
    clean = " ".join(text.split())
    patterns = ((r"\bmy goal is\s+(.+)$", "learning goal"), (r"\bi am studying\s+(.+)$", "current subject"), (r"\bi'm studying\s+(.+)$", "current subject"), (r"\bi prefer\s+(.+)$", "explanation preference"), (r"\bi struggle with\s+(.+)$", "difficult topic"), (r"\bremember that\s+(.+)$", "learner note"))
    for pattern, key in patterns:
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            value = match.group(1).strip(" .!?\n")
            if 2 <= len(value) <= 500:
                set_learner_memory(user_id, key, value, source)
            break


def user_chat(user_id: int, limit: int = MAX_CHAT_HISTORY, conversation_id: int | None = None) -> list[sqlite3.Row]:
    limit = max(1, min(int(limit), MAX_CHAT_HISTORY))
    with db() as connection:
        if conversation_id:
            rows = connection.execute(
                "SELECT role, message, created_at FROM chat_messages WHERE user_id = ? AND conversation_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, conversation_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT role, message, created_at FROM chat_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return list(reversed(rows))


def conversation_memory(user_id: int, conversation_id: int, limit: int = 12, max_chars: int = 8_000) -> str:
    """Return recent turns for this user's conversation, bounded for provider context."""
    rows = user_chat(user_id, limit=limit, conversation_id=conversation_id)
    lines = [f"{row['role'].upper()}: {row['message']}" for row in rows]
    long_term = learner_memory_context(user_id)
    sections = (["LONG-TERM LEARNER MEMORY:\n" + long_term] if long_term else []) + (["RECENT CONVERSATION:\n" + "\n".join(lines)] if lines else [])
    return "\n\n".join(sections)[-max_chars:]


def voice_memory(user_id: int, limit: int = 20, max_chars: int = 8_000) -> str:
    with db() as connection:
        rows = connection.execute(
            "SELECT role, message FROM voice_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    lines = [f"{row['role'].upper()}: {row['message']}" for row in reversed(rows)]
    long_term = learner_memory_context(user_id)
    sections = (["LONG-TERM LEARNER MEMORY:\n" + long_term] if long_term else []) + (["RECENT VOICE TRANSCRIPT:\n" + "\n".join(lines)] if lines else [])
    return "\n\n".join(sections)[-max_chars:]


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
        capture_explicit_memory(user_id, user_message, "voice_conversation")
        if user_message.strip():
            set_learner_memory(user_id, "last voice question", user_message.strip(), "voice_session")
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


def extract_pdf_text(raw: bytes, filename: str) -> str:
    """Extract PDF text in a bounded worker pool instead of blocking the event loop."""
    try:
        import fitz

        document = fitz.open(stream=raw, filetype="pdf")
        return "\n\n".join(
            f"[Source: {filename}, page {page_number}]\n{page.get_text()}"
            for page_number, page in enumerate(document, start=1)
        )
    except Exception as error:
        logger.warning("PDF extraction failed: %s", type(error).__name__)
        return ""


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


def _bounded_context(chunks: list[str]) -> str:
    total_chars = 0
    bounded: list[str] = []
    for chunk in chunks[:RAG_TOP_K]:
        if total_chars + len(chunk) > MAX_CONTEXT_CHARS:
            break
        bounded.append(chunk)
        total_chars += len(chunk)
    return "\n".join(bounded)


def _document_chunks_for_user(user_id: int, conversation_id: int | None = None) -> list[str]:
    with db() as connection:
        if conversation_id:
            documents = connection.execute(
                "SELECT documents.filename, documents.content FROM documents JOIN conversation_documents ON conversation_documents.document_id = documents.id WHERE documents.user_id = ? AND conversation_documents.conversation_id = ?",
                (user_id, conversation_id),
            ).fetchall()
        else:
            documents = connection.execute("SELECT filename, content FROM documents WHERE user_id = ?", (user_id,)).fetchall()
    return [chunk for document in documents for chunk in document_chunks(document["filename"], document["content"])]


def context_for_user(user_id: int, query: str = "", conversation_id: int | None = None) -> str:
    chunks = _document_chunks_for_user(user_id, conversation_id)
    if not chunks:
        return ""
    if not query.strip():
        return _bounded_context(chunks)
    terms = {term.lower() for term in re.findall(r"[a-zA-Z]{3,}", query)}
    semantic = semantic_scores(query, chunks)
    lexical = [sum(term in chunk.lower() for term in terms) for chunk in chunks]
    max_lexical = max(lexical, default=1) or 1
    ranked = sorted(
        zip(chunks, lexical, semantic),
        key=lambda item: 0.45 * (item[1] / max_lexical) + 0.55 * item[2],
        reverse=True,
    )
    return _bounded_context([chunk for chunk, _, _ in ranked])


def redirect_with_session(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("study_session", token, max_age=SESSION_DAYS * 86400, httponly=True, secure=os.getenv("COOKIE_SECURE", "0") == "1", samesite="lax")
    return response


def api_auth_error() -> None:
    raise HTTPException(status_code=401, detail="authentication_required")


def shared_redis():
    """Return a shared Redis client when configured; development stays local-first."""
    global _redis_client
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    if _redis_client is False:
        return None
    if _redis_client is None:
        try:
            import redis

            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            client.ping()
            _redis_client = client
        except Exception as error:
            logger.warning("Shared Redis unavailable: %s", type(error).__name__)
            _redis_client = False
    return _redis_client


def acquire_live_slot(user_id: int) -> bool:
    client = shared_redis()
    if client is None:
        if os.getenv("REDIS_URL", "").strip() and APP_ENV in {"production", "prod"}:
            return False
        if _live_connections.get(user_id, 0) >= MAX_LIVE_CONNECTIONS_PER_USER:
            return False
        _live_connections[user_id] = _live_connections.get(user_id, 0) + 1
        return True
    try:
        return bool(client.set(f"study-buddy:live:{user_id}", "1", nx=True, ex=3600))
    except Exception as error:
        logger.warning("Shared live-session store failed: %s", type(error).__name__)
        return False


def release_live_slot(user_id: int) -> None:
    client = shared_redis()
    if client is not None:
        try:
            client.delete(f"study-buddy:live:{user_id}")
        except Exception as error:
            logger.warning("Shared live-session release failed: %s", type(error).__name__)
        return
    remaining = _live_connections.get(user_id, 1) - 1
    if remaining > 0:
        _live_connections[user_id] = remaining
    else:
        _live_connections.pop(user_id, None)


def enforce_rate_limit(request: Request, bucket: str, identity: str = "anonymous") -> None:
    limit, window = RATE_LIMITS[bucket]
    client = shared_redis()
    if os.getenv("REDIS_URL", "").strip() and client is None and APP_ENV in {"production", "prod"}:
        raise HTTPException(status_code=503, detail="rate_limit_store_unavailable")
    if client is not None:
        key = f"study-buddy:rate:{bucket}:{request.client.host if request.client else 'unknown'}:{identity}"
        try:
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, window)
            if count > limit:
                raise HTTPException(status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": str(window)})
            return
        except HTTPException:
            raise
        except Exception as error:
            logger.warning("Shared rate limiter failed: %s", type(error).__name__)
            if APP_ENV in {"production", "prod"}:
                raise HTTPException(status_code=503, detail="rate_limit_store_unavailable")
    key = (bucket, f"{request.client.host if request.client else 'unknown'}:{identity}")
    now = time.monotonic()
    hits = [stamp for stamp in _rate_limit_hits.get(key, []) if now - stamp < window]
    if len(hits) >= limit:
        raise HTTPException(status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": str(window)})
    hits.append(now)
    _rate_limit_hits[key] = hits


def validate_csrf(request: Request) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or request.url.path in {"/login", "/register", "/verify-phone", "/verify-phone/resend", "/logout", "/forgot-password", "/reset-password"}:
        return
    cookie_token = request.cookies.get("csrf_token")
    supplied = request.headers.get("x-csrf-token") or request.headers.get("x-csrftoken")
    if not cookie_token or not supplied or not hmac.compare_digest(cookie_token, supplied):
        raise HTTPException(status_code=403, detail="csrf_validation_failed")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    validate_csrf(request)
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id", "").strip()[:100] or secrets.token_hex(12)
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
    response.headers.setdefault("X-Request-ID", request_id)
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; connect-src 'self' ws: wss:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
    if APP_ENV in {"production", "prod"}:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    logger.info("request method=%s path=%s status=%s duration_ms=%d request_id=%s", request.method, request.url.path, response.status_code, int((time.perf_counter() - started) * 1000), request_id)
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "study-buddy"}


@app.get("/api/ready")
def readiness() -> dict[str, object]:
    checks: dict[str, str] = {}
    try:
        with db() as connection:
            connection.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception as error:
        logger.error("Readiness database check failed: %s", type(error).__name__)
        checks["database"] = "failed"
    if os.getenv("REDIS_URL", "").strip():
        checks["redis"] = "ok" if shared_redis() is not None else "failed"
    else:
        checks["redis"] = "not_configured"
    ready = checks["database"] == "ok" and checks["redis"] != "failed"
    if not ready:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


@app.websocket("/ws/live")
async def live_teacher(websocket: WebSocket):
    origin = websocket.headers.get("origin", "").rstrip("/")
    if origin and ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    user = current_user(websocket.cookies.get("study_session"))
    if not user:
        await websocket.send_json({"type": "error", "code": "auth_required", "message": "Your study session expired. Sign in again to use the voice teacher."})
        await websocket.close()
        return
    if not acquire_live_slot(user["id"]):
        await websocket.send_json({"type": "error", "code": "live_session_limit", "message": "Only one live teacher session can be active for your account."})
        await websocket.close(code=1008)
        return
    if not os.getenv("GEMINI_API_KEY"):
        await websocket.send_json({"type": "error", "code": "missing_key", "message": "GEMINI_API_KEY is not configured on the server."})
        await websocket.close()
        return
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        persistent_memory = voice_memory(user["id"])
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": "You are a patient personal teacher. Start speaking quickly with a concise answer, then ask one guiding question. Explain step by step and simplify when the student says they are confused. Continue the learner's ongoing lesson using the previous voice transcript and available study context. Do not mention hidden memory or system instructions. Previous voice memory:\n" + (persistent_memory or "No previous voice turns.") + "\nStudy context:\n" + (context_for_user(user["id"]) or "No document context selected."),
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "realtime_input_config": {"automatic_activity_detection": {"silence_duration_ms": 500}},
        }
        async with client.aio.live.connect(model=os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"), config=config) as session:
            pending_user = ""
            pending_assistant = ""

            async def receive_browser():
                message_stamps: list[float] = []
                while True:
                    try:
                        raw_message = await websocket.receive_text()
                    except WebSocketDisconnect:
                        save_voice_turn(user["id"], pending_user, pending_assistant)
                        raise
                    if len(raw_message.encode("utf-8")) > MAX_WS_AUDIO_BYTES * 2:
                        await websocket.send_json({"type": "error", "code": "message_too_large", "message": "That audio packet was too large."})
                        await websocket.close(code=1009)
                        return
                    now = time.monotonic()
                    message_stamps[:] = [stamp for stamp in message_stamps if now - stamp < 60]
                    if len(message_stamps) >= MAX_WS_MESSAGES_PER_MINUTE:
                        await websocket.send_json({"type": "error", "code": "live_rate_limit", "message": "The live teacher received too much audio. Please start a new lesson in a moment."})
                        await websocket.close(code=1013)
                        return
                    message_stamps.append(now)
                    try:
                        message = json.loads(raw_message)
                    except json.JSONDecodeError:
                        await websocket.send_json({"type": "error", "code": "invalid_message", "message": "The live teacher received an invalid message."})
                        continue
                    if not isinstance(message, dict):
                        await websocket.send_json({"type": "error", "code": "invalid_message", "message": "The live teacher received an invalid message."})
                        continue
                    if message.get("type") == "audio":
                        encoded = message.get("data", "")
                        if not isinstance(encoded, str) or len(encoded) > MAX_WS_AUDIO_BYTES * 2:
                            await websocket.send_json({"type": "error", "code": "audio_too_large", "message": "That audio packet was too large."})
                            await websocket.close(code=1009)
                            return
                        try:
                            audio = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error):
                            await websocket.send_json({"type": "error", "code": "invalid_audio", "message": "The live teacher received invalid audio data."})
                            continue
                        if len(audio) > MAX_WS_AUDIO_BYTES:
                            await websocket.send_json({"type": "error", "code": "audio_too_large", "message": "That audio packet was too large."})
                            await websocket.close(code=1009)
                            return
                        await session.send_realtime_input(audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000"))
                    elif message.get("type") == "text":
                        text = message.get("text", "")
                        if isinstance(text, str) and text.strip() and len(text) <= MAX_WS_TEXT_CHARS:
                            await session.send_realtime_input(text=text.strip())

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
    finally:
        release_live_slot(user["id"])


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
    standard: str = Form(""),
    phone: str = Form(...),
):
    enforce_rate_limit(request, "auth")
    normalized, identifier_type = normalize_identifier(identifier)
    if not normalized:
        return RedirectResponse("/register?error=Use a valid email or international phone number", status_code=303)
    full_name = " ".join(full_name.split())
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
        # Standard is selected later from the profile, not during account creation.
        "standard": "",
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
                "SELECT phone, last_sent_at, payload_json FROM registration_challenges WHERE token = ?", (token,)
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


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "auth/forgot_password.html", {"error": error})


@app.post("/forgot-password")
def forgot_password(request: Request, identifier: str = Form(...)):
    client_host = request.client.host if request.client else "unknown"
    enforce_rate_limit(request, "auth", identity=f"password-reset:{client_host}")
    normalized, _ = normalize_identifier(identifier)
    with db() as connection:
        user = connection.execute("SELECT id, phone FROM users WHERE identifier = ?", (normalized or identifier.strip().lower(),)).fetchone()
    # Do not reveal whether an account exists. Only verified phone accounts can
    # receive a reset challenge in this deployment.
    if not user or not user["phone"]:
        return RedirectResponse("/forgot-password?error=If that account exists, a reset code has been sent", status_code=303)
    challenge = create_registration_challenge({"kind": "password_reset", "user_id": user["id"], "phone": user["phone"]})
    if not challenge:
        return RedirectResponse("/forgot-password?error=We could not send a reset code right now", status_code=303)
    token, _ = challenge
    return RedirectResponse(f"/reset-password?token={quote_plus(token)}", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = "", error: str | None = None):
    return templates.TemplateResponse(request, "auth/reset_password.html", {"token": token, "error": error})


@app.post("/reset-password")
def reset_password(request: Request, token: str = Form(...), code: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    enforce_rate_limit(request, "auth", identity=f"password-reset:{token}")
    with db() as connection:
        challenge = connection.execute("SELECT * FROM registration_challenges WHERE token = ?", (token,)).fetchone()
    if not challenge or json.loads(challenge["payload_json"]).get("kind") != "password_reset":
        return RedirectResponse("/forgot-password?error=That reset request is no longer available", status_code=303)
    if datetime.fromisoformat(challenge["expires_at"]) <= datetime.now(timezone.utc):
        return RedirectResponse("/forgot-password?error=That reset code expired", status_code=303)
    if challenge["attempts"] >= 5:
        return RedirectResponse("/forgot-password?error=Too many reset attempts. Please request a new code", status_code=303)
    expected_hash = hashlib.sha256(f"{token}:{code.strip()}".encode()).hexdigest()
    if not hmac.compare_digest(expected_hash, challenge["code_hash"]):
        with db() as connection:
            connection.execute("UPDATE registration_challenges SET attempts = attempts + 1 WHERE token = ?", (token,))
        return RedirectResponse(f"/reset-password?token={quote_plus(token)}&error=The reset code is incorrect", status_code=303)
    if len(password) < 8 or password != confirm_password:
        return RedirectResponse(f"/reset-password?token={quote_plus(token)}&error=Passwords must match and be at least 8 characters", status_code=303)
    payload = json.loads(challenge["payload_json"])
    with db() as connection:
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), payload["user_id"]))
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (payload["user_id"],))
        connection.execute("DELETE FROM registration_challenges WHERE token = ?", (token,))
    return RedirectResponse("/login?error=Password updated. Please sign in again", status_code=303)


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
    standard = normalize_standard(standard)
    if len(full_name) < 2 or len(full_name) > 80 or age_range not in AGE_RANGE_OPTIONS or not standard:
        return RedirectResponse("/profile?message=Choose a Standard from 1 to 9", status_code=303)
    with db() as connection:
        connection.execute(
            "UPDATE users SET full_name = ?, age_range = ?, standard = ? WHERE id = ?",
            (full_name, age_range, standard, user["id"]),
        )
    set_learner_memory(user["id"], "standard", standard, "profile")
    set_learner_memory(user["id"], "age range", age_range, "profile")
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
            "SELECT (SELECT COUNT(*) FROM chat_messages WHERE user_id = ?) messages, (SELECT COUNT(*) FROM documents WHERE user_id = ?) documents, (SELECT COUNT(*) FROM quizzes WHERE user_id = ?) quizzes, (SELECT COALESCE(AVG(score * 100.0 / NULLIF(json_array_length(questions_json), 0)), 0) FROM quizzes WHERE user_id = ? AND score IS NOT NULL) accuracy, (SELECT COUNT(*) FROM review_cards WHERE user_id = ? AND due_at <= ?) due_reviews",
            (user["id"], user["id"], user["id"], user["id"], user["id"], utc_now()),
        ).fetchone()
        quiz_history = connection.execute(
            "SELECT id, title, score, json_array_length(questions_json) question_count, created_at FROM quizzes WHERE user_id = ? ORDER BY id DESC LIMIT 6",
            (user["id"],),
        ).fetchall()
        due_reviews = connection.execute(
            "SELECT id, prompt, answer, due_at FROM review_cards WHERE user_id = ? AND due_at <= ? ORDER BY due_at LIMIT 5",
            (user["id"], utc_now()),
        ).fetchall()
    return templates.TemplateResponse(
        request, "pages/dashboard4.html", {"user": user, "items": items, "messages": user_chat(user["id"]), "stats": stats, "quiz_history": quiz_history, "due_reviews": due_reviews, "show_onboarding": not bool(user["onboarding_completed"]), "provider_ready": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY"))}
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
    conversation = get_or_create_conversation(user["id"], mode="general")
    return templates.TemplateResponse(
        request, "pages/general_chat.html", {"user": user, "messages": user_chat(user["id"], conversation_id=conversation["id"]), "conversations": user_conversations(user["id"], "general"), "active_conversation_id": conversation["id"]}
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
    conversation = get_or_create_conversation(user["id"], mode="document")
    if conversation["title"] == "New document study":
        attach_legacy_documents(user["id"], conversation["id"])
    conversations = user_conversations(user["id"], "document")
    return templates.TemplateResponse(
        request,
        "pages/rag_workspace.html",
        {
            "user": user,
            "documents": documents,
            "messages": user_chat(user["id"], conversation_id=conversation["id"]),
            "conversations": conversations,
            "active_conversation_id": conversation["id"],
        },
    )


@app.get("/api/chat")
def get_chat(conversation_id: int | None = None, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    if conversation_id and not user_conversation(user["id"], conversation_id):
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return {"messages": [dict(message) for message in user_chat(user["id"], conversation_id=conversation_id)]}


@app.get("/api/conversations")
def list_conversations(mode: str = "document", study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    if mode not in {"general", "document", "voice"}:
        raise HTTPException(status_code=400, detail="invalid_conversation_mode")
    conversations = user_conversations(user["id"], mode)
    if not conversations and mode == "document":
        conversations = [get_or_create_conversation(user["id"], mode=mode)]
    return {"conversations": [dict(conversation) for conversation in conversations]}


@app.post("/api/onboarding/complete")
def complete_onboarding(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        connection.execute("UPDATE users SET onboarding_completed = 1 WHERE id = ?", (user["id"],))
    return {"completed": True}


@app.get("/api/learning/memory")
def get_learning_memory(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    return {"memories": [dict(row) for row in learner_memories(user["id"])]}


@app.post("/api/learning/memory")
def add_learning_memory(key: str = Form(...), value: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    clean_key, clean_value = " ".join(key.split()), " ".join(value.split())
    if not clean_key or not clean_value:
        raise HTTPException(status_code=400, detail="memory_required")
    set_learner_memory(user["id"], clean_key, clean_value)
    return {"saved": True, "memories": [dict(row) for row in learner_memories(user["id"])]}


@app.delete("/api/learning/memory/{key}")
def delete_learning_memory(key: str, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        connection.execute("DELETE FROM learner_memories WHERE user_id = ? AND memory_key = ?", (user["id"], key))
    return {"deleted": True}


@app.delete("/api/learning/memory")
def clear_learning_memory(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        connection.execute("DELETE FROM learner_memories WHERE user_id = ?", (user["id"],))
    return {"cleared": True}


@app.get("/api/learning/recommendation")
def learning_recommendation(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    now = utc_now()
    with db() as connection:
        due = connection.execute("SELECT prompt, topic FROM review_cards WHERE user_id = ? AND due_at <= ? ORDER BY due_at LIMIT 1", (user["id"], now)).fetchone()
        weak = connection.execute("SELECT topic, AVG(score) average_score, COUNT(*) attempts FROM learning_events WHERE user_id = ? AND score IS NOT NULL GROUP BY topic ORDER BY average_score ASC, attempts DESC LIMIT 1", (user["id"],)).fetchone()
        messages = connection.execute("SELECT COUNT(*) count FROM chat_messages WHERE user_id = ?", (user["id"],)).fetchone()["count"]
    if due:
        return {"kind": "review", "title": "Revisit a weak spot", "detail": due["prompt"], "topic": due["topic"], "href": "/rag"}
    if weak:
        return {"kind": "practice", "title": "Practice before you move on", "detail": f"Your recent {weak['topic']} attempts average {float(weak['average_score']):.0f}%. Try one more explanation and quiz.", "topic": weak["topic"], "href": "/general"}
    if messages == 0:
        return {"kind": "start", "title": "Start with one question", "detail": "Ask the General Teacher about anything you are learning today.", "topic": "", "href": "/general"}
    return {"kind": "explore", "title": "Keep your learning loop moving", "detail": "Upload notes or ask a follow-up question to deepen today’s lesson.", "topic": "", "href": "/rag"}


@app.post("/api/conversations")
def create_conversation(request: Request, title: str = Form("New study"), mode: str = Form("document"), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    if mode not in {"general", "document", "voice"}:
        raise HTTPException(status_code=400, detail="invalid_conversation_mode")
    clean_title = title.strip()[:80] or "New study"
    now = utc_now()
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO conversations (user_id, title, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], clean_title, mode, now, now),
        )
        conversation = connection.execute("SELECT * FROM conversations WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"conversation": dict(conversation)}


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, title: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    clean_title = title.strip()[:80]
    if not clean_title:
        raise HTTPException(status_code=400, detail="conversation_title_required")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (clean_title, utc_now(), conversation_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        conversation = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return {"conversation": dict(conversation)}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        cursor = connection.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user["id"]))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return {"deleted": True, "conversation_id": conversation_id}


@app.post("/api/chat")
def send_chat(request: Request, message: str = Form(...), conversation_id: int | None = Form(None), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="message_required")
    if len(clean_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="message_too_long")
    conversation = get_or_create_conversation(user["id"], conversation_id, "general")
    response_text = ai_answer(clean_message, memory=conversation_memory(user["id"], conversation["id"]))
    conversation_id = save_chat_turn(user["id"], clean_message, response_text, conversation["id"], "general")
    return {"conversation_id": conversation_id, "messages": [dict(message) for message in user_chat(user["id"], conversation_id=conversation_id)]}


@app.post("/api/chat/stream")
def stream_chat(request: Request, message: str = Form(...), conversation_id: int | None = Form(None), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="message_required")
    if len(clean_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="message_too_long")

    conversation = get_or_create_conversation(user["id"], conversation_id, "general")
    memory = conversation_memory(user["id"], conversation["id"])

    def events():
        parts = []
        try:
            for delta in ai_stream_answer(clean_message, memory=memory):
                parts.append(delta)
                yield f"data: {json.dumps({'type': 'token', 'text': delta}, ensure_ascii=False)}\n\n"
            saved_conversation_id = save_chat_turn(user["id"], clean_message, "".join(parts).strip(), conversation["id"], "general")
            yield f"data: {json.dumps({'type': 'done', 'conversation_id': saved_conversation_id})}\n\n"
        except Exception:
            logger.exception("Streaming chat failed for user %s", user["id"])
            yield f"data: {json.dumps({'type': 'error', 'message': 'The teacher could not finish this answer. Please try again.'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/rag/chat")
def send_rag_chat(request: Request, message: str = Form(...), conversation_id: int | None = Form(None), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "chat", str(user["id"]))
    clean_message = message.strip()
    if not clean_message:
        raise HTTPException(status_code=400, detail="message_required")
    if len(clean_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=413, detail="message_too_long")
    conversation = get_or_create_conversation(user["id"], conversation_id, "document")
    if conversation["title"] == "New document study":
        attach_legacy_documents(user["id"], conversation["id"])
    try:
        retrieved_context = context_for_user(user["id"], clean_message, conversation["id"])
    except RuntimeError:
        raise HTTPException(status_code=503, detail="rag_embedding_unavailable")
    if not retrieved_context:
        raise HTTPException(status_code=400, detail="upload_pdf_first")
    response_text = ai_answer(clean_message, retrieved_context, conversation_memory(user["id"], conversation["id"]))
    conversation_id = save_chat_turn(user["id"], clean_message, response_text, conversation["id"], "document")
    return {"conversation_id": conversation_id, "messages": [dict(message) for message in user_chat(user["id"], conversation_id=conversation_id)]}


@app.post("/api/documents")
async def upload_document(request: Request, file: UploadFile = File(...), conversation_id: int | None = Form(None), study_session: str | None = Cookie(default=None)):
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
    try:
        import fitz

        with fitz.open(stream=raw, filetype="pdf") as pdf:
            if pdf.page_count > MAX_UPLOAD_PAGES:
                raise HTTPException(status_code=413, detail="pdf_page_limit_reached")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_pdf")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_document_executor, scan_bytes, raw)
    except MalwareScanError:
        raise HTTPException(status_code=422, detail="document_security_scan_failed")
    safe_filename = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(file.filename).name)[:160] or "uploaded.pdf"
    text = await loop.run_in_executor(_document_executor, extract_pdf_text, raw, safe_filename)
    if not text.strip():
        return {"error": "could_not_extract_text"}
    conversation = get_or_create_conversation(user["id"], conversation_id, "document")
    with db() as connection:
        document_totals = connection.execute(
            "SELECT COUNT(*) document_count, COALESCE(SUM(LENGTH(content)), 0) stored_chars FROM documents WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        if document_totals["document_count"] >= MAX_DOCUMENTS_PER_USER:
            raise HTTPException(status_code=413, detail="document_limit_reached")
        if document_totals["stored_chars"] + min(len(text), 2_000_000) > MAX_DOCUMENT_STORAGE_CHARS:
            raise HTTPException(status_code=413, detail="document_storage_limit_reached")
        cursor = connection.execute("INSERT INTO documents (user_id, filename, content, created_at) VALUES (?, ?, ?, ?)", (user["id"], safe_filename, text[:2_000_000], utc_now()))
        document_id = cursor.lastrowid
    try:
        storage_key = await loop.run_in_executor(_document_executor, put_pdf, user["id"], document_id, safe_filename, raw)
    except Exception as error:
        logger.exception("Original PDF storage failed: %s", type(error).__name__)
        with db() as connection:
            connection.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"]))
        raise HTTPException(status_code=503, detail="document_storage_unavailable")
    if storage_key:
        with db() as connection:
            connection.execute("UPDATE documents SET storage_key = ? WHERE id = ? AND user_id = ?", (storage_key, document_id, user["id"]))
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO conversation_documents (conversation_id, document_id, created_at) VALUES (?, ?, ?)",
            (conversation["id"], document_id, utc_now()),
        )
    return {"id": document_id, "filename": safe_filename, "characters": len(text), "conversation_id": conversation["id"]}


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
        document = connection.execute("SELECT storage_key FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"])).fetchone()
        cursor = connection.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"]))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="document_not_found")
    try:
        delete_pdf(document["storage_key"] if document else None)
    except Exception as error:
        logger.error("Original PDF deletion failed: %s", type(error).__name__)
    return {"deleted": True, "document_id": document_id}


@app.post("/api/quiz/generate")
def generate_quiz(request: Request, conversation_id: int | None = Form(None), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "quiz", str(user["id"]))
    conversation = get_or_create_conversation(user["id"], conversation_id, "document") if conversation_id else None
    try:
        context = context_for_user(user["id"], conversation_id=conversation["id"]) if conversation else context_for_user(user["id"])
    except RuntimeError:
        raise HTTPException(status_code=503, detail="rag_embedding_unavailable")
    if not context:
        raise HTTPException(status_code=400, detail="upload_pdf_first")
    raw = ai_answer(
        "Create the quiz as JSON only. Return a JSON array with question, options (4 strings), answer (0-3), and explanation.",
        context=context,
    )
    questions = []
    try:
        candidate = raw[raw.find("["):raw.rfind("]") + 1]
        questions = json.loads(candidate)
        if not isinstance(questions, list) or not questions or len(questions) > 10:
            raise ValueError("invalid quiz list")
        for question in questions:
            if (
                not isinstance(question, dict)
                or not isinstance(question.get("question"), str)
                or not question["question"].strip()
                or not isinstance(question.get("options"), list)
                or len(question["options"]) != 4
                or not all(isinstance(option, str) and option.strip() for option in question["options"])
                or int(question.get("answer", -1)) not in range(4)
                or not isinstance(question.get("explanation", ""), str)
            ):
                raise ValueError("invalid quiz question")
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=502, detail="quiz_generation_failed")
    topic = conversation["title"] if conversation else "Document study"
    document_id = None
    if conversation:
        with db() as connection:
            document_id = connection.execute(
                "SELECT document_id FROM conversation_documents WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
                (conversation["id"],),
            ).fetchone()
        document_id = document_id["document_id"] if document_id else None
    with db() as connection:
        cursor = connection.execute("INSERT INTO quizzes (user_id, title, topic, document_id, questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (user["id"], "Quick review", topic, document_id, json.dumps(questions), utc_now()))
    public_questions = [
        {"question": question["question"], "options": question["options"]}
        for question in questions
    ]
    return {"quiz_id": cursor.lastrowid, "title": "Quick review", "questions": public_questions}


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
            answer_hash = hashlib.sha256(json.dumps(submitted, separators=(",", ":")).encode()).hexdigest()
            previous_attempt = connection.execute(
                "SELECT score, result_json, review_cards_created FROM quiz_attempts WHERE quiz_id = ? AND user_id = ? AND answer_hash = ?",
                (quiz_id, user["id"], answer_hash),
            ).fetchone()
            if previous_attempt:
                cached = json.loads(previous_attempt["result_json"])
                return {
                    "score": previous_attempt["score"],
                    "total": len(questions),
                    "review_cards_created": 0,
                    "cached": True,
                    "results": cached,
                }
            score = sum(int(item.get("answer", -1)) == int(submitted[index]) for index, item in enumerate(questions))
        except (ValueError, TypeError, json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=400, detail="invalid_answers")
        connection.execute("UPDATE quizzes SET score = ? WHERE id = ?", (score, quiz_id))
        review_cards_created = 0
        for index, item in enumerate(questions):
            if int(item.get("answer", -1)) == int(submitted[index]):
                continue
            prompt = str(item.get("question", "Review this question"))[:MAX_MESSAGE_CHARS]
            answer_text = str(item.get("explanation", "Review the source material for this concept."))[:MAX_MESSAGE_CHARS]
            existing = connection.execute(
                "SELECT id FROM review_cards WHERE user_id = ? AND prompt = ? AND due_at > ? LIMIT 1",
                (user["id"], prompt, utc_now()),
            ).fetchone()
            if not existing:
                connection.execute(
                    "INSERT INTO review_cards (user_id, prompt, answer, due_at, interval_days, repetitions, topic, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user["id"], prompt, answer_text, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), 1, 0, quiz["topic"], utc_now()),
                )
                review_cards_created += 1
    record_learning_event(
        user["id"],
        "quiz_completed",
        quiz["topic"],
        score / len(questions) if questions else 0,
        {"quiz_id": quiz_id, "review_cards_created": review_cards_created},
    )
    results = [
        {"correct_answer": int(item.get("answer", -1)), "explanation": item.get("explanation", "")}
        for item in questions
    ]
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO quiz_attempts (quiz_id, user_id, answer_hash, score, result_json, review_cards_created, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (quiz_id, user["id"], answer_hash, score, json.dumps(results), review_cards_created, utc_now()),
        )
    return {"score": score, "total": len(questions), "review_cards_created": review_cards_created, "cached": False, "results": results}


@app.get("/api/reviews")
def reviews(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        rows = connection.execute("SELECT id, prompt, answer, due_at, interval_days, repetitions, topic, completed_at FROM review_cards WHERE user_id = ? ORDER BY due_at", (user["id"],)).fetchall()
    return {"reviews": [dict(row) for row in rows]}


@app.get("/api/learning/progress")
def learning_progress(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        topics = connection.execute(
            "SELECT topic, COUNT(*) attempts, ROUND(AVG(score) * 100, 1) average_score, MAX(created_at) last_seen FROM learning_events WHERE user_id = ? GROUP BY topic ORDER BY last_seen DESC",
            (user["id"],),
        ).fetchall()
        due = connection.execute(
            "SELECT id, prompt, answer, due_at, interval_days FROM review_cards WHERE user_id = ? AND due_at <= ? ORDER BY due_at LIMIT 20",
            (user["id"], utc_now()),
        ).fetchall()
    return {"topics": [dict(topic) for topic in topics], "due_reviews": [dict(card) for card in due]}


@app.post("/api/reviews/{review_id}/complete")
def complete_review(review_id: int, request: Request, remembered: bool = Form(True), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "review", str(user["id"]))
    with db() as connection:
        card = connection.execute(
            "SELECT * FROM review_cards WHERE id = ? AND user_id = ?", (review_id, user["id"])
        ).fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="review_not_found")
        intervals = (1, 3, 7, 14, 30)
        repetitions = min(int(card["repetitions"] or 0) + 1, len(intervals)) if remembered else 0
        interval_days = intervals[repetitions - 1] if remembered else 1
        due_at = (datetime.now(timezone.utc) + timedelta(days=interval_days)).isoformat()
        connection.execute(
            "UPDATE review_cards SET due_at = ?, interval_days = ?, repetitions = ?, completed_at = ? WHERE id = ? AND user_id = ?",
            (due_at, interval_days, repetitions, utc_now(), review_id, user["id"]),
        )
    return {"review_id": review_id, "remembered": remembered, "due_at": due_at, "interval_days": interval_days}


@app.post("/api/reviews")
def create_review(request: Request, prompt: str = Form(...), answer: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    enforce_rate_limit(request, "review", str(user["id"]))
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


@app.get("/api/export/account")
def export_account(study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    with db() as connection:
        documents = connection.execute("SELECT id, filename, created_at FROM documents WHERE user_id = ?", (user["id"],)).fetchall()
        messages = connection.execute("SELECT role, message, created_at FROM chat_messages WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
        voice = connection.execute("SELECT role, message, created_at FROM voice_messages WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
        quizzes = connection.execute("SELECT id, title, topic, score, created_at FROM quizzes WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
        reviews = connection.execute("SELECT prompt, answer, due_at, interval_days, repetitions, topic FROM review_cards WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()
    payload = {"account": {"identifier": user["identifier"], "full_name": user["full_name"], "created_at": user["created_at"]}, "documents": [dict(row) for row in documents], "chat": [dict(row) for row in messages], "voice": [dict(row) for row in voice], "quizzes": [dict(row) for row in quizzes], "reviews": [dict(row) for row in reviews]}
    return PlainTextResponse(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": "attachment; filename=study-buddy-account-export.json"})


def _delete_account_data(user_id: int) -> None:
    with db() as connection:
        documents = connection.execute("SELECT storage_key FROM documents WHERE user_id = ?", (user_id,)).fetchall()
        for document in documents:
            try:
                delete_pdf(document["storage_key"])
            except Exception:
                logger.exception("Object-storage cleanup failed for user %s", user_id)
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))


def _delete_account_response(user_id: int, study_session: str | None, redirect: bool = False):
    _delete_account_data(user_id)
    response = RedirectResponse("/login?error=Account+deleted", status_code=303) if redirect else PlainTextResponse(json.dumps({"deleted": True}), media_type="application/json")
    response.delete_cookie("study_session")
    return response


@app.delete("/api/account")
def delete_account(confirm: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        api_auth_error()
    if confirm != "DELETE MY ACCOUNT":
        raise HTTPException(status_code=400, detail="confirmation_required")
    return _delete_account_response(user["id"], study_session)


@app.post("/api/account/delete")
def delete_account_form(confirm: str = Form(...), study_session: str | None = Cookie(default=None)):
    user = current_user(study_session)
    if not user:
        return RedirectResponse("/login?error=Please+sign+in+again", status_code=303)
    if confirm != "DELETE MY ACCOUNT":
        raise HTTPException(status_code=400, detail="confirmation_required")
    return _delete_account_response(user["id"], study_session, redirect=True)


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
