"""Structural checks on the templates.

Every page rendered fine and every route test passed while the dashboard was
showing its job table inside the page header, because a status code and a
substring search cannot see where content landed. These assert the shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "studio" / "templates"
PAGES = sorted(p for p in TEMPLATES.glob("*.html") if p.name != "base.html")


def block(text: str, name: str) -> str | None:
    m = re.search(r"\{%\s*block " + name + r"\s*%\}(.*?)\{%\s*endblock\s*%\}", text, re.S)
    return m.group(1) if m else None


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
class TestPageStructure:
    def test_extends_the_base(self, page):
        assert page.read_text(encoding="utf-8").lstrip().startswith('{% extends "base.html" %}')

    def test_title_and_crumb_hold_only_text(self, page):
        # A careless replace once injected a whole table into these, which put
        # the job list in the header on every page.
        text = page.read_text(encoding="utf-8")
        for name in ("title", "crumb"):
            body = block(text, name)
            if body is None:
                continue
            assert "<h2>" not in body, f"{name} block contains a section heading"
            assert "<div" not in body, f"{name} block contains a div"
            assert len(body) < 200, f"{name} block is too long to be a label"

    def test_exactly_one_body_block(self, page):
        text = page.read_text(encoding="utf-8")
        assert text.count("{% block body %}") == 1

    def test_nothing_of_substance_outside_a_block(self, page):
        # Jinja silently discards markup outside blocks in a child template, so
        # content that ends up there vanishes without an error.
        text = page.read_text(encoding="utf-8")
        outside = re.sub(r"\{%\s*block .*?\{%\s*endblock\s*%\}", "", text, flags=re.S)
        outside = re.sub(r"\{%.*?%\}", "", outside, flags=re.S).strip()
        assert "<" not in outside, f"markup outside any block: {outside[:80]!r}"

    def test_balanced_block_tags(self, page):
        text = page.read_text(encoding="utf-8")
        assert len(re.findall(r"\{%\s*block ", text)) == len(re.findall(r"\{%\s*endblock", text))


class TestRenderable:
    def test_every_template_compiles(self):
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
        env.filters["duration"] = str
        for page in TEMPLATES.glob("*.html"):
            env.get_template(page.name)

