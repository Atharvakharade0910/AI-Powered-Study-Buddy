# Interview Notes

## One-sentence description

Study Buddy is a multi-user AI learning workspace that turns private PDFs into an interactive tutoring and assessment experience.

## Architecture story

1. FastAPI receives authenticated chat, document, quiz, and voice requests.
2. SQLite stores users, sessions, chats, documents, quizzes, and review cards with user IDs on every record.
3. PyMuPDF extracts page-aware PDF chunks and a hybrid retriever combines keyword overlap with local sentence embeddings to select relevant passages.
4. The provider adapter sends bounded context to Gemini first or Groq as a fallback.
5. The browser renders answers, MCQs, scores, and explanations in the same learning flow.

## Honest tradeoffs

- Retrieval is hybrid: exact keyword relevance protects formulas and named entities, while local embeddings handle paraphrased student questions. Embeddings are computed lazily with `all-MiniLM-L6-v2`; no hosted vector database is required.
- SQLite and the in-process rate limiter are suitable for a local prototype, not a multi-worker deployment.
- Gemini Live voice mode requires a Gemini API key and a compatible live model.
- Email verification and password reset need an external delivery provider.

## Resume bullet

Built an AI-powered FastAPI study platform with authenticated multi-user workspaces, PDF-grounded tutoring, interactive MCQ scoring, Gemini Live voice teaching, user-scoped SQLite persistence, CSRF protection, upload validation, rate limits, exports, and automated behavior tests.

## Demo path

Register or sign in -> upload a PDF -> ask for a page-specific explanation -> click Make MCQs -> submit answers -> show score and explanations -> open the dashboard quiz history -> demonstrate voice mode if Gemini is configured.
