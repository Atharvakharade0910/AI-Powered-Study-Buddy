"""Import a syllabus page into the source-backed curriculum table.

This intentionally accepts an explicit official URL instead of crawling the
whole internet. Run it once per board/state/class/subject source and review
the resulting record before enabling it for learners.

Example:
  python scripts/scrape_curriculum.py --url https://... --board CBSE \
    --standard "Standard 10" --subject Science --year 2025-26
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, init_db  # noqa: E402


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            value = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if value:
                self.parts.append(value)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "StudyBuddyCurriculumBot/1.0 (+source-import)"})
    with urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
    if "pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        import fitz
        document = fitz.open(stream=raw, filetype="pdf")
        return "\n".join(f"[Page {page.number + 1}] {page.get_text('text')}" for page in document)
    if "html" not in content_type.lower() and not url.lower().endswith(('.html', '.htm')):
        raise ValueError("The source must be an official HTML syllabus page or PDF")
    parser = TextParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return " ".join(parser.parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--board", choices=["CBSE", "State Board"], required=True)
    parser.add_argument("--state", default=None)
    parser.add_argument("--standard", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--chapter", default="Syllabus overview")
    parser.add_argument("--topic", default="")
    parser.add_argument("--stream", default="")
    parser.add_argument("--year", default="")
    parser.add_argument("--title", default="Official curriculum source")
    parser.add_argument("--review-status", choices=["pending", "approved"], default="pending")
    args = parser.parse_args()
    if args.board == "State Board" and not args.state:
        parser.error("--state is required for State Board imports")
    content = fetch_text(args.url)
    if len(content) < 80:
        raise ValueError("The source page did not contain enough readable curriculum text")
    init_db()
    with db() as connection:
        connection.execute(
            "INSERT INTO curriculum_chunks (board, state, standard, subject, stream, chapter, topic, content, source_url, source_title, academic_year, review_status, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (board, state, standard, subject, chapter, source_url) DO UPDATE SET stream = excluded.stream, topic = excluded.topic, content = excluded.content, source_title = excluded.source_title, academic_year = excluded.academic_year, review_status = excluded.review_status, content_hash = excluded.content_hash, created_at = excluded.created_at",
            (args.board, args.state if args.board == "State Board" else None, args.standard, args.subject, args.stream or None, args.chapter, args.topic or None, content[:24000], args.url, args.title, args.year, args.review_status, hashlib.sha256(content.encode()).hexdigest(), datetime.now(timezone.utc).isoformat()),
        )
    print(f"Imported {args.board} {args.state or ''} {args.standard} {args.subject} from {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
