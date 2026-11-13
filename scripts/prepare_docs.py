"""Inject the current GitHub repository and Pages URL into the upload artifact."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = ("index.html", "architecture.html", "installation.html", "support.html", "404.html")
META_REPOSITORY = re.compile(r'(<meta name="gh-repository" content=")[^"]*("\s*/>)')
META_PAGES = re.compile(r'(<meta name="gh-pages-url" content=")[^"]*("\s*/>)')
CANONICAL = re.compile(r'(<link rel="canonical" href=")[^"]*("\s*/>)')


def replace_once(pattern: re.Pattern[str], text: str, replacement: str, description: str) -> str:
    updated, count = pattern.subn(lambda match: match.group(1) + replacement + match.group(2), text, count=1)
    if count != 1:
        raise SystemExit(f"Expected exactly one {description} placeholder; found {count}")
    return updated


def prepare() -> str:
    repository = os.environ.get("REPOSITORY", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise SystemExit("REPOSITORY must be a GitHub owner/name value")
    owner, name = repository.split("/", 1)
    if name.casefold() == f"{owner}.github.io".casefold():
        pages_url = f"https://{owner}.github.io/"
    else:
        pages_url = f"https://{owner}.github.io/{name}/"
    for filename in PAGES:
        page = DOCS / filename
        html = page.read_text(encoding="utf-8")
        html = replace_once(META_REPOSITORY, html, repository, f"{filename} repository")
        html = replace_once(META_PAGES, html, pages_url, f"{filename} Pages URL")
        html = replace_once(CANONICAL, html, pages_url, f"{filename} canonical URL")
        page.write_text(html, encoding="utf-8")
    return pages_url


if __name__ == "__main__":
    url = prepare()
    print(f"Prepared Pages site for {os.environ['REPOSITORY']} at {url}")
