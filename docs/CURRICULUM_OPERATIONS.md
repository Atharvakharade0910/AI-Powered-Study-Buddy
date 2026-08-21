# Curriculum operations

The curriculum system is source-backed and review-gated. Subject buttons are
available from the learner profile, but an LLM or voice answer receives
official curriculum context only from rows marked `approved`.

## Import a source

Run the importer once per official board/state/class/stream/subject source:

```powershell
python scripts/scrape_curriculum.py `
  --url "https://official.example/syllabus.pdf" `
  --board "CBSE" `
  --standard "Standard 10" `
  --subject "Science" `
  --chapter "Science syllabus" `
  --year "2026-27"
```

For State Board records, include `--state`. For Standards 11 and 12, include
`--stream`.

Imports default to `pending`. This is intentional. A curriculum administrator
must review the source before approval through:

```text
GET   /api/admin/curriculum?review_status=pending
PATCH /api/admin/curriculum/{id}
      review_status=approved
```

Only approved rows are eligible for text and voice tutoring. Each row retains
the source URL, source title, academic year, content hash, chapter, topic, and
review timestamp.

## Production checklist

- Configure `ALLOWED_ORIGINS` with the exact HTTPS public origin.
- Import and review the official curriculum for every supported board/state.
- Do not mark a source approved until its class, subject, chapter, language,
  and academic year have been checked by an educator.
- Back up PostgreSQL and object storage before bulk imports.
- Run imports as a background job for large PDF collections; do not run bulk
  imports in the web request process.
- Keep source content and learner-uploaded documents in separate retrieval
  namespaces.
