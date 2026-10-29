"""Tests for scripts/licence_audit.py (no strong copyleft; weak copyleft only as named exceptions)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "licence_audit.py"
_spec = importlib.util.spec_from_file_location("licence_audit", _SCRIPT)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules["licence_audit"] = audit
_spec.loader.exec_module(audit)

Cat = audit.Category
Pkg = audit.Package


class TestClassify:
    @pytest.mark.parametrize(
        "expression, expected",
        [
            ("MIT", Cat.PERMISSIVE),
            ("Apache-2.0 OR BSD-3-Clause", Cat.PERMISSIVE),
            ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", Cat.PERMISSIVE),
            ("LGPL-3.0-only", Cat.WEAK),
            ("MPL-2.0 AND MIT", Cat.WEAK),
            ("GPL-3.0-or-later", Cat.STRONG),
            ("AGPL-3.0-only", Cat.STRONG),
            ("SSPL-1.0", Cat.STRONG),
            ("EUPL-1.2", Cat.STRONG),
            ("OSL-3.0", Cat.STRONG),
            ("MIT AND GPL-2.0-only", Cat.STRONG),
            ("GPL-2.0-only OR MIT", Cat.PERMISSIVE),
            ("(MIT OR GPL-3.0-only) AND LGPL-2.1-only", Cat.WEAK),
            ("Apache-2.0 WITH LLVM-exception", Cat.PERMISSIVE),
        ],
    )
    def test_spdx_expressions(self, expression, expected):
        assert audit.classify(expression, [], None) is expected

    @pytest.mark.parametrize(
        "classifiers, expected",
        [
            (["License :: OSI Approved :: MIT License"], Cat.PERMISSIVE),
            (
                ["License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)"],
                Cat.WEAK,
            ),
            (["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"], Cat.STRONG),
            (["License :: OSI Approved :: GNU Affero General Public License v3"], Cat.STRONG),
            (["License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)"], Cat.WEAK),
            (
                [
                    "License :: OSI Approved :: BSD License",
                    "License :: OSI Approved :: Apache Software License",
                ],
                Cat.PERMISSIVE,
            ),
        ],
    )
    def test_classifiers(self, classifiers, expected):
        assert audit.classify(None, classifiers, None) is expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Apache 2.0 License", Cat.PERMISSIVE),
            ("BSD-3-Clause, Apache-2.0, dependency licenses", Cat.PERMISSIVE),
            ("GNU General Public License v3", Cat.STRONG),
            ("Some custom terms", Cat.UNKNOWN),
        ],
    )
    def test_licence_text(self, text, expected):
        assert audit.classify(None, [], text) is expected

    def test_nothing_is_unknown(self):
        assert audit.classify(None, [], None) is Cat.UNKNOWN

    def test_expression_wins_over_classifiers(self):
        assert (
            audit.classify(
                "MIT", ["License :: OSI Approved :: GNU General Public License v3"], None
            )
            is Cat.PERMISSIVE
        )


class TestPolicy:
    def test_permissive_passes(self):
        assert audit.check([Pkg("fastapi", "1.0", Cat.PERMISSIVE, "MIT")], {}) == []

    def test_strong_copyleft_fails(self):
        problems = audit.check([Pkg("html2text", "1.0", Cat.STRONG, "GPL-3.0-or-later")], {})
        assert len(problems) == 1 and "html2text" in problems[0] and "strong" in problems[0]

    def test_strong_copyleft_cannot_be_an_exception(self):
        problems = audit.check(
            [Pkg("html2text", "1.0", Cat.STRONG, "GPL-3.0-or-later")],
            {"html2text": "someone wanted it"},
        )
        assert len(problems) == 1

    def test_weak_copyleft_needs_a_named_exception(self):
        pkg = Pkg("pystray", "0.19", Cat.WEAK, "LGPL-3.0")
        assert len(audit.check([pkg], {})) == 1
        assert audit.check([pkg], {"pystray": "tray icon, unmodified"}) == []

    def test_unknown_licence_fails(self):
        problems = audit.check([Pkg("mystery", "1.0", Cat.UNKNOWN, "")], {})
        assert len(problems) == 1 and "mystery" in problems[0]

    def test_exception_names_are_normalised(self):
        pkg = Pkg("psycopg-binary", "3.3", Cat.WEAK, "LGPL-3.0-only")
        assert audit.check([pkg], {"psycopg_binary": "driver"}) == []


class TestNamedExceptions:
    def test_the_reviewed_list(self):
        assert set(audit.EXCEPTIONS) == {
            "psycopg",
            "psycopg-binary",
            "psycopg-pool",
            "certifi",
            "pystray",
            "python-xlib",
            "tqdm",
        }
        assert all(reason.strip() for reason in audit.EXCEPTIONS.values())


class TestInstalledClosure:
    def test_spark_runtime_dependencies_pass(self):
        """The audit of this environment's runtime closure is clean (what CI runs)."""
        packages = audit.runtime_closure("cognisn-spark")
        names = {p.name for p in packages}
        assert {"fastapi", "psycopg", "aiosqlite", "alembic", "markdownify"} <= names
        # Extras a requirement asks for are followed: psycopg[binary,pool].
        assert {"psycopg-binary", "psycopg-pool"} <= {n.lower() for n in names}
        assert "pytest" not in names  # dev extra excluded
        assert audit.check(packages, audit.EXCEPTIONS) == []
