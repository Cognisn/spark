"""Admin tables: one shared scrolling wrapper, and the audit trail's one-line rows.

These need no database: the audit rows partial is rendered on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

from spark.web.endpoints.audit import _details_formatted

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
RULE = re.compile(r"\.app-table-responsive\s*\{[^}]*overflow-x:\s*auto[^}]*\}")


def _render(rows: list[dict[str, object]]) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(WEB / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template("_audit_rows.html").render(rows=rows, actors={})


def _row(details: object, row_id: int = 7) -> dict[str, object]:
    return {
        "id": row_id,
        "occurred_at": "2026-11-24 01:02:03",
        "action": "warden.identity.sign_in",
        "outcome": "success",
        "actor": "",
        "resource": "",
        "source_ip": "192.0.2.1",
        "details": "{}",
        "details_formatted": _details_formatted(details),
    }


def test_the_wrapper_rule_is_in_base_css_once_and_not_in_connections_css() -> None:
    base = (WEB / "static" / "css" / "base.css").read_text(encoding="utf-8")
    connections = (WEB / "static" / "css" / "connections.css").read_text(encoding="utf-8")
    assert len(RULE.findall(base)) == 1
    assert "max-width: 100%" in RULE.search(base).group(0)  # type: ignore[union-attr]
    assert "app-table-responsive" not in connections


def test_the_audit_template_no_longer_breaks_details_anywhere() -> None:
    for name in ("audit.html", "_audit_rows.html"):
        assert "break-all" not in (WEB / "templates" / name).read_text(encoding="utf-8")


def test_a_row_with_nested_details_has_a_view_button_and_formatted_json() -> None:
    page = _render([_row({"a": {"b": [1, 2]}, "z": "x"})])
    button = re.search(r"<button[^>]*data-audit-view[^>]*>", page)
    assert button is not None
    assert 'aria-expanded="false"' in button.group(0)
    assert 'aria-controls="audit-details-7"' in button.group(0)
    assert re.search(r'<tr class="audit-details-row" id="audit-details-7" hidden>', page)
    assert "  &#34;a&#34;: {\n    &#34;b&#34;: [\n      1," in page
    assert "data-audit-copy" in page


def test_a_row_without_details_has_no_view_button() -> None:
    page = _render([_row({})])
    assert "data-audit-view" not in page


def test_details_containing_markup_are_escaped() -> None:
    payload = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    page = _render([_row({"note": payload})])
    assert "<script>alert(1)" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_every_table_in_a_template_or_built_in_script_sits_in_the_shared_wrapper() -> None:
    sources = [*(WEB / "templates").glob("*.html"), WEB / "static" / "js" / "connections.js"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        tables = len(re.findall(r"<table\b|h\('table'", text))
        wrapped = len(re.findall(r"app-table-responsive", text))
        assert tables == wrapped, f"{source.name}: {tables} tables, {wrapped} wrappers"
