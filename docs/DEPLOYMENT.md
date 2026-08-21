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
- Set `ALLOWED_ORIGINS` to the exact public HTTPS origin used by the browser voice client.
- Set `MAX_UPLOAD_PAGES` to an operationally safe value; the default is 200.

## Before horizontal scaling

The production stack now uses PostgreSQL and Redis, but horizontal scaling still requires validation in the target environment. Before adding replicas, complete and test:

1. A tested SQLite-to-PostgreSQL data migration for existing users.
2. Shared object storage for original PDFs in the target environment.
3. A durable document job queue for extraction and embeddings.
4. Backup restoration, load, browser end-to-end, and provider-contract tests.
5. Malware/document scanning before extraction, durable backups with restore drills, and error-monitoring alerts.

The production guard refuses to start without PostgreSQL and Redis. This prevents a deployment that appears scalable while silently falling back to local-only persistence or rate-limit state.

## Public deployment with Render

The repository includes `render.yaml` for a public Docker deployment with a Render web service, managed PostgreSQL, and Render Key Value. Render automatically provides an HTTPS `onrender.com` URL and can redeploy the configured branch after successful CI checks.

1. Open the Render Blueprint flow and connect the GitHub repository.
2. Select the `agent/study-buddy-twilio` branch, or change `branch` in `render.yaml` before creating the Blueprint.
3. Enter the values for every environment variable marked `sync: false`, especially `GEMINI_API_KEY`, Twilio credentials, and object-storage credentials.
4. Create the Blueprint and wait for `/api/ready` to pass.

The Blueprint uses free resources for an initial public preview. Free Render Postgres databases can expire and do not include backups, while free Key Value instances are in-memory and can lose cache/rate-limit state on restart. For a real multi-user production launch, upgrade the web service and datastores, configure S3-compatible object storage for uploaded PDFs, and enable backups before sharing the URL widely.

Password recovery is available after registration verification: the learner enters the account identifier at `/forgot-password`, receives a reset code on the verified phone, and completes the reset at `/reset-password`. This requires Twilio (or another implemented SMS provider) in production; the development code provider is rejected by the production startup guard.
