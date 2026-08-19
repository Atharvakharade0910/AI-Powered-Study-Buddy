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

For a public HTTPS deployment, use `docker-compose.production.yml` behind a reverse proxy or managed ingress. Set `COOKIE_SECURE=1`, terminate TLS before the container, and never use the development SMS provider.

## Operational checks

- `/api/health` is a liveness check.
- `/api/ready` checks the database and Redis when `REDIS_URL` is configured.
- Keep the application at one worker while SQLite is enabled.
- Persist `/app/data` and back it up before upgrades.
- Do not commit `.env`, database files, or provider credentials.
- Run `python -m pytest -q` before building an image.

## Before horizontal scaling

The application still stores business data in SQLite. Redis now provides shared rate-limit state, but Redis does not make SQLite safe for multiple application replicas. Before adding workers or replicas, complete and test:

1. PostgreSQL persistence for all application tables.
2. Alembic migrations, including a data migration from SQLite.
3. Shared object storage for original PDFs.
4. A durable document job queue for extraction and embeddings.
5. Shared voice-session coordination or a dedicated voice service.
6. Backup restoration, load, browser end-to-end, and provider-contract tests.

The Docker and CI files intentionally do not claim that SQLite has been converted to PostgreSQL. This boundary prevents a deployment that appears scalable while silently losing data or violating user isolation across replicas.
