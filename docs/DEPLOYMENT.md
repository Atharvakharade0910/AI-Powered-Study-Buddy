# Deployment runbook

## Current supported deployment

The repository supports a controlled single-container deployment with one application worker, SQLite persistence, and Redis-backed rate limiting:

```powershell
Copy-Item .env.example .env
# Configure GEMINI_API_KEY or GROQ_API_KEY and real Twilio values.
docker compose up --build -d
Invoke-WebRequest http://127.0.0.1:8000/api/health
Invoke-WebRequest http://127.0.0.1:8000/api/ready
```

For a public HTTPS deployment, copy `.env.production.example` to `.env.production`, replace every placeholder, and use `docker-compose.production.yml` behind a reverse proxy or managed ingress. Set `COOKIE_SECURE=1`, terminate TLS before the container, and never use the development SMS provider.

## Operational checks

- `/api/health` is a liveness check.
- `/api/ready` checks the database and Redis when `REDIS_URL` is configured.
- PostgreSQL is used by the production Compose stack; SQLite remains available for local development.
- Persist `/app/data` and back it up before upgrades.
- Do not commit `.env`, database files, or provider credentials.
- Run `python -m pytest -q` before building an image.

## Before horizontal scaling

The production stack now uses PostgreSQL and Redis, but horizontal scaling still requires validation in the target environment. Before adding replicas, complete and test:

1. A tested SQLite-to-PostgreSQL data migration for existing users.
2. Shared object storage for original PDFs in the target environment.
3. A durable document job queue for extraction and embeddings.
4. Backup restoration, load, browser end-to-end, and provider-contract tests.

The production guard refuses to start without PostgreSQL and Redis. This prevents a deployment that appears scalable while silently falling back to local-only persistence or rate-limit state.
