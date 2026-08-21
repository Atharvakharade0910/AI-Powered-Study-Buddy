# AI-Powered Study Buddy

AI study workspace built with FastAPI. Users can ask a general teacher, upload private PDFs for document-grounded tutoring, generate interactive MCQs, review quiz history, and use a Gemini Live voice-teacher mode.

## Highlights

- Multi-user registration, login, sessions, and user-scoped data
- Scrypt password hashing and CSRF protection for state-changing requests
- Hybrid PDF retrieval using PyMuPDF, keyword relevance, and local sentence embeddings
- Gemini and Groq text-provider boundary with local configuration
- Interactive MCQ generation, scoring, and explanations
- Gemini Live voice teaching over WebSockets
- Document management, chat/voice export, review cards, analytics, and admin view
- Upload, message, quiz, and authentication rate limits
- Phone-based password recovery, account export, and confirmed account deletion
- Sanitized upload names, a configurable 200-page PDF limit, request IDs, and duration logs
- FastAPI smoke and behavior tests

## Project structure

```text
AI-Powered Study Buddy/
|-- app.py                 # FastAPI routes, authentication, persistence, WebSocket
|-- ai_provider.py         # Gemini/Groq text-provider adapter
|-- requirements.txt
|-- .env.example           # Configuration names without secrets
|-- data/                  # Local SQLite runtime data (ignored by Git)
|-- static/                # Shared CSS and images
|-- templates/             # Auth, dashboard, learning modes, and admin views
|-- tests/                 # Smoke and authenticated behavior tests
`-- docs/                  # Architecture and interview notes
```

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, create an account, upload a PDF, and ask a question. Configure at least `GEMINI_API_KEY` or `GROQ_API_KEY` in `.env` for AI responses. The default text tutor model is `gemini-3.6-flash`. Gemini is required for the live voice teacher, which uses the separate `GEMINI_LIVE_MODEL` setting because Gemini 3.6 Flash does not support the Live API.

### Phone verification

Local development uses the `dev` SMS provider and displays the generated code on the verification page. For real phone delivery, configure Twilio without committing credentials:

```env
APP_ENV=production
SMS_PROVIDER=twilio
PHONE_VERIFICATION_DEV_MODE=0
TWILIO_ACCOUNT_SID=your-account-sid
TWILIO_API_KEY_SID=your-api-key-sid
TWILIO_API_KEY_SECRET=your-api-key-secret
# Alternatively, use TWILIO_AUTH_TOKEN instead of the API key pair.
# TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_FROM_PHONE=your-twilio-number
```

The API key pair is used when both API key variables are present. Keep the Account SID, API key secret, Auth Token, and `.env` file private.

The application refuses to start in production when development verification is enabled or when the provider is still `dev`. Registration creates an account only after the code is verified. Codes expire after 10 minutes; failed attempts and resend requests are rate-limited.

Run checks with:

```powershell
python -m pytest -q
python -m compileall -q app.py ai_provider.py
```

## Data and privacy

The default database is `data/study_buddy.db`. Uploaded PDF text and prompts are stored per user. If a cloud provider key is configured, relevant document context is sent to that provider. Configure a local provider before handling sensitive material.

## Production notes

The local app intentionally uses SQLite. The production Compose stack uses PostgreSQL, Alembic migrations, Redis-backed rate limits/live-session coordination, HTTPS expectations, `COOKIE_SECURE=1`, and private S3-compatible storage when configured. Password recovery is implemented through the verified phone number and a real SMS provider. External malware/document scanning, backups with restore drills, and error-monitoring alert delivery remain production operating requirements.

The repository includes a production-oriented Docker image, Docker Compose files, a CI workflow, PostgreSQL/Alembic support, Redis-backed rate limiting, shared live-session coordination, a `/api/ready` dependency check, private object-storage integration, and bounded PDF extraction workers. The local Docker Compose file remains SQLite-based and starts Uvicorn directly; the production Compose file starts PostgreSQL and runs `alembic upgrade head` before the application.

For the current controlled deployment path:

```powershell
Copy-Item .env.example .env
# Fill in the AI and Twilio production values, then set APP_ENV=production,
# PHONE_VERIFICATION_DEV_MODE=0, SMS_PROVIDER=twilio, and COOKIE_SECURE=1.
docker compose -f docker-compose.production.yml up --build -d
```

The compose setup provides Redis for shared rate limits. HTTPS must be terminated by a reverse proxy or managed ingress in front of the container; `COOKIE_SECURE=1` requires that browser traffic is actually HTTPS.

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the supported single-container runbook and the remaining PostgreSQL/object-storage migration gates before horizontal scaling.

Document study fails closed when no usable document context is available; it does not silently fall back to general chat. Stored documents are bounded to 50 per account and 20 million extracted characters per account. Chat history returned to the browser is bounded to the most recent 100 messages.

The Gemini Live WebSocket limits each account to one active session, validates audio payloads, caps individual audio packets, and applies a per-connection message budget. The browser surfaces provider, authentication, rate-limit, upload, and retrieval errors instead of treating non-success responses as empty results.

For browser voice security, set `ALLOWED_ORIGINS` to the exact public HTTPS origin(s), comma-separated. Leaving it empty is useful for local development but allows any browser origin to attempt the WebSocket handshake; authentication and the live-session limit still apply.

Signed-in learners can download `/api/export/account` from their profile. Account deletion is available at `/api/account` for API clients (DELETE with `confirm=DELETE MY ACCOUNT`) or through the profile button, and removes the user-scoped database records plus configured original PDFs from object storage.

Quiz answers and explanations are kept server-side until submission. The initial quiz response contains only questions and options; grading returns the correct answers and explanations after the attempt.

New accounts receive a one-time dashboard welcome tour explaining the General Teacher, Document Study, Voice Teacher, quizzes, and adaptive review flow. The tour attempts a short browser voice introduction, includes a manual voice fallback for autoplay restrictions, and can be replayed from the dashboard through “How it works.”

## Supported learners

Study Buddy is currently designed for learners in Standard 1 through Standard 9. New registrations and profile updates accept only Standards 1–9. Existing account records are preserved during this product-scope change, but their next profile update must use a supported Standard.
