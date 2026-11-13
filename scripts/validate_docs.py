"""Validate local references in the static GitHub Pages documentation."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REQUIRED = (
    "index.html",
    "architecture.html",
    "installation.html",
    "support.html",
    "site.css",
    "site.js",
    "favicon.svg",
    "404.html",
    "robots.txt",
    "README.md",
)


class ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.language = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "html":
            self.language = attributes.get("lang", "")
        for attribute in ("href", "src"):
            value = attributes.get(attribute)
            if value:
                self.references.append(value)


def validate() -> None:
    missing = [name for name in REQUIRED if not (DOCS / name).is_file()]
    if missing:
        raise SystemExit(f"Missing documentation files: {', '.join(missing)}")

    pages = ("index.html", "architecture.html", "installation.html", "support.html", "404.html")
    failures: list[str] = []
    references_checked = 0
    for filename in pages:
        html = (DOCS / filename).read_text(encoding="utf-8")
        for marker, message in (
            ('name="gh-repository" content=""', "repository"),
            ('name="gh-pages-url" content=""', "Pages URL"),
            ('rel="canonical" href=""', "canonical URL"),
        ):
            if marker not in html:
                raise SystemExit(f"{filename}: missing deployment-time {message} placeholder")
        if "your-org/true-hq-audio" in html:
            raise SystemExit(f"{filename}: placeholder repository URL found")
        parser = ReferenceParser()
        parser.feed(html)
        if filename != "404.html" and parser.language != "es":
            raise SystemExit(f"{filename}: main documentation must declare lang=es")
        references_checked += len(parser.references)
        for reference in parser.references:
            parsed = urlparse(reference)
            if parsed.scheme or reference.startswith("//") or reference.startswith("#"):
                continue
            target = (DOCS / parsed.path).resolve()
            if target != DOCS.resolve() and DOCS.resolve() not in target.parents:
                failures.append(f"local reference escapes docs/: {reference}")
                continue
            if parsed.path:
                if target.is_dir():
                    target = target / "index.html"
                if not target.is_file():
                    failures.append(f"missing local file {reference}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Documentation valid: {len(REQUIRED)} required files, {references_checked} references")


if __name__ == "__main__":
    validate()
