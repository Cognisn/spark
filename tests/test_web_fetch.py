"""web_fetch turns HTML into readable Markdown (links kept, images dropped, no wrapping)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spark.tools import web

PAGE = """<html><head><title>T</title><script>var x = 1;</script></head><body>
<h1>Example heading</h1>
<p>Read the <a href="https://www.example.com/report">full report</a> for detail.
This paragraph is deliberately long enough that a converter wrapping lines at eighty columns would split it somewhere in the middle.</p>
<img src="https://www.example.com/chart.png" alt="chart">
<ul><li>First point</li><li>Second point</li></ul>
</body></html>"""


@pytest.fixture
def fake_get(monkeypatch):
    def install(text: str, content_type: str = "text/html; charset=utf-8") -> None:
        import httpx

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: SimpleNamespace(text=text, headers={"content-type": content_type}),
        )

    return install


def test_html_becomes_markdown(fake_get):
    fake_get(PAGE)
    out = web._web_fetch("https://www.example.com/", 10_000)
    assert "# Example heading" in out
    assert "[full report](https://www.example.com/report)" in out
    assert "chart.png" not in out
    assert "First point" in out and "Second point" in out
    assert "var x = 1" not in out
    long_line = next(line for line in out.splitlines() if "deliberately long" in line)
    assert "split it somewhere in the middle." in long_line


def test_long_pages_are_truncated(fake_get):
    fake_get("<p>" + "word " * 5000 + "</p>")
    out = web._web_fetch("https://www.example.com/", 500)
    assert out.endswith("\n... (truncated)")
    assert len(out) <= 500 + len("\n... (truncated)")


def test_json_is_returned_as_is(fake_get):
    fake_get('{"a": 1}', "application/json")
    assert web._web_fetch("https://www.example.com/a.json", 100) == '{"a": 1}'


def test_html2text_is_not_a_dependency():
    """html2text is GPL-3.0-or-later; SPARK's runtime dependencies are not strong copyleft."""
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert not any(d.lower().startswith("html2text") for d in data["project"]["dependencies"])
