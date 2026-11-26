"""The live suite's coverage registry, checked without a stack (the default suite runs it).

Every live case is registered once in :mod:`tests.live.coverage`, every entry names
a test that exists, and ``docs/live-integration-coverage.md`` is the registry as
rendered, so the document cannot drift from the suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.live import coverage
from tests.live.conftest import OFFLINE

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def live_tests() -> set[str]:
    """Every test function of the live suite, as ``tests/live/<file>::<name>``."""
    found: set[str] = set()
    for path in sorted(HERE.glob("test_*.py")):
        if path.name in OFFLINE:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.add(f"tests/live/{path.name}::{node.name}")
    return found


def test_every_live_test_is_registered() -> None:
    registered = {case.test for case in coverage.CASES}
    missing = sorted(live_tests() - registered)
    assert not missing, f"register these in tests/live/coverage.py: {missing}"


def test_every_entry_names_a_live_test() -> None:
    unknown = sorted({case.test for case in coverage.CASES} - live_tests())
    assert not unknown, f"no such live test: {unknown}"


def test_each_case_is_registered_once_under_its_own_id() -> None:
    ids = [case.id for case in coverage.CASES]
    tests = [case.test for case in coverage.CASES]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert len(tests) == len(set(tests)), sorted({t for t in tests if tests.count(t) > 1})


def test_every_entry_says_what_it_proves() -> None:
    for case in coverage.CASES:
        assert case.suite in coverage.SUITES, case
        assert case.scope.strip() and case.proves.strip(), case
        assert "\n" not in case.proves and "|" not in case.proves, case


def test_the_document_is_the_registry_rendered() -> None:
    document = REPO / coverage.DOCUMENT
    assert document.is_file(), f"run python -m tests.live.coverage to write {coverage.DOCUMENT}"
    assert (
        document.read_text("utf-8") == coverage.render()
    ), f"{coverage.DOCUMENT} is out of date: run python -m tests.live.coverage"


def test_the_conftest_marks_every_live_test_but_the_offline_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a stack every live test is integration-marked and skipped, and the
    offline checks are neither, so CI's ``-m "not integration"`` still runs them."""
    from tests.live import conftest

    class Item:
        def __init__(self, name: str) -> None:
            self.fspath = HERE / name
            self.marks: list[str] = []

        def add_marker(self, mark: pytest.MarkDecorator) -> None:
            self.marks.append(mark.name)

    monkeypatch.delenv(conftest.STATE_ENV, raising=False)
    items = [Item(name) for name in (*sorted(OFFLINE), "test_projects_live.py")]
    conftest.pytest_collection_modifyitems(None, items)  # type: ignore[arg-type]
    assert [i.marks for i in items[:-1]] == [[] for _ in OFFLINE]
    assert items[-1].marks == ["integration", "skip"]
