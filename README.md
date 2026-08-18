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

The local app intentionally uses SQLite and an in-process rate limiter. A production deployment should use PostgreSQL, HTTPS, `COOKIE_SECURE=1`, a shared rate-limit store, database migrations, password reset delivery, structured observability, and a malware/document scanning pipeline.
