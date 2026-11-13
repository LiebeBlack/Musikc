"""Static documentation integrity checks."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


class ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.language = ""
        self.title = ""
        self.has_main = False

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        if tag == "html":
            self.language = attributes.get("lang", "")
        if tag == "main":
            self.has_main = True
        for key in ("href", "src"):
            value = attributes.get(key)
            if value:
                self.references.append(value)

    def handle_data(self, data: str) -> None:
        if self.title:
            return


def test_documentation_has_required_static_assets():
    for name in (
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
    ):
        assert (DOCS / name).is_file(), name


def test_landing_page_language_semantics_and_version():
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    parser = ReferenceParser()
    parser.feed(html)
    assert parser.language == "es"
    assert parser.has_main
    assert "2.5.9" in html
    assert "your-org/true-hq-audio" not in html
    assert "gh-repository" in html
    assert "data-release-link" in html
    for filename in ("architecture.html", "installation.html", "support.html"):
        page = (DOCS / filename).read_text(encoding="utf-8")
        parsed = ReferenceParser()
        parsed.feed(page)
        assert parsed.language == "es"
        assert parsed.has_main


def test_local_html_references_resolve():
    for name in ("index.html", "architecture.html", "installation.html", "support.html", "404.html"):
        parser = ReferenceParser()
        parser.feed((DOCS / name).read_text(encoding="utf-8"))
        for reference in parser.references:
            parsed = urlparse(reference)
            if parsed.scheme or reference.startswith("//") or reference.startswith("#"):
                continue
            target = (DOCS / parsed.path).resolve()
            assert DOCS.resolve() in target.parents or target == DOCS.resolve(), reference
            if parsed.path:
                if target.is_dir():
                    target = target / "index.html"
                assert target.is_file(), reference


def test_pages_workflow_validates_deploys_and_uses_permissions():
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    for token in (
        "pages: write",
        "id-token: write",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
        "requirements-dev.txt",
        "python -m pytest -q tests",
        "scripts/validate_docs.py",
        "scripts/prepare_docs.py",
        "docs",
    ):
        assert token in workflow


def test_pages_preparation_uses_real_repository_and_escapes_replacement(monkeypatch):
    import scripts.prepare_docs as prepare_docs

    pages = ("index.html", "architecture.html", "installation.html", "support.html", "404.html")
    originals = {name: (DOCS / name).read_text(encoding="utf-8") for name in pages}
    try:
        monkeypatch.setenv("REPOSITORY", "audio-lab/true-hq-audio")
        url = prepare_docs.prepare()
        assert url == "https://audio-lab.github.io/true-hq-audio/"
        for name in pages:
            updated = (DOCS / name).read_text(encoding="utf-8")
            assert 'content="audio-lab/true-hq-audio"' in updated
            assert 'content="https://audio-lab.github.io/true-hq-audio/"' in updated
            assert 'href="https://audio-lab.github.io/true-hq-audio/"' in updated
            assert "your-org/true-hq-audio" not in updated
    finally:
        for name, original in originals.items():
            (DOCS / name).write_text(original, encoding="utf-8")


def test_public_site_has_no_external_runtime_assets():
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    css = (DOCS / "site.css").read_text(encoding="utf-8")
    js = (DOCS / "site.js").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in html + css
    assert "cdn.jsdelivr.net" not in html + css + js
    assert "@import url('')" not in css
    assert re.search(r"<script src=\"site\.js\" defer>", html)
