# Study Buddy project structure

The project keeps runtime code at the root and groups user-facing assets by purpose:

- `app.py` owns FastAPI routes, authentication, SQLite access, REST endpoints, and the Gemini Live WebSocket.
- `ai_provider.py` is the provider boundary for text tutoring. It reads Gemini first and Groq as the configured fallback.
- `templates/auth/` contains login, registration, and optional intro screens.
- `templates/pages/` contains the home dashboard and the three learning modes: general teacher, document study, and voice teacher.
- `templates/admin/` contains the protected admin view.
- `static/css/` contains shared styling; `static/images/` contains logos and visual assets.
- `data/` contains local runtime data. The SQLite database is intentionally ignored by Git.
- `tests/` contains fast regression checks for public routes, authentication guards, assets, and frontend handlers.
- `.env.example` documents configuration names without exposing local secrets.

The main user flow is:

`/login` → `/dashboard` → `/general`, `/rag`, or `/voice`

All chat, document, quiz, review, and analytics API calls remain scoped to the authenticated user.
