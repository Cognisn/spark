"""Licence audit of SPARK's runtime dependencies.

Policy: no strong copyleft (GPL, AGPL, SSPL, EUPL, OSL) anywhere in the runtime
dependency closure. Weak copyleft (LGPL, MPL, EPL, CDDL) only as a named,
reviewed exception for a package used unmodified and installed separately.
A package whose licence cannot be determined fails until it is reviewed.

The closure is read from installed distribution metadata, starting at the
project's own requirements without extras (so the dev tools are excluded),
with environment markers evaluated for this interpreter and platform.

Usage: python scripts/licence_audit.py [--root cognisn-spark]
Exit status 0 when the closure complies, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from importlib.metadata import PackageMetadata, PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

__all__ = [
    "BUNDLED_ASSETS",
    "EXCEPTIONS",
    "Category",
    "Package",
    "check",
    "check_bundled_assets",
    "classify",
    "main",
    "runtime_closure",
]


class Category(IntEnum):
    """Ordered by how restrictive the licence is."""

    PERMISSIVE = 0
    WEAK = 1
    UNKNOWN = 2
    STRONG = 3


# Named, reviewed weak-copyleft exceptions. Every entry needs its reason.
EXCEPTIONS: dict[str, str] = {
    "psycopg": "LGPL-3.0: PostgreSQL driver (server mode), unmodified, installed separately",
    "psycopg-binary": "LGPL-3.0: psycopg's binary build, unmodified, installed separately",
    "psycopg-pool": "LGPL-3.0: psycopg's connection pool, unmodified, installed separately",
    "certifi": "MPL-2.0: CA bundle used by httpx and requests, unmodified",
    "pystray": "LGPL-3.0: system tray icon (tray daemon only), unmodified",
    "python-xlib": "LGPL-2.0-or-later: pystray's X11 backend on Linux, unmodified",
    "tqdm": "MPL-2.0: progress bars, transitive via sentence-transformers, unmodified",
}

# Third-party files shipped inside the wheel (not Python packages). Each needs its licence text
# beside it, a permissive or font licence, and a line in THIRD-PARTY-NOTICES.md. A library
# (one with a "version") is pinned: the notice names its version, and every file it ships
# has its SHA-256 here, checked against the file in the tree. Where a library offers a choice
# of licences, "licence" is the one SPARK takes and "offered" says what was offered.
ROOT = Path(__file__).resolve().parent.parent
NOTICES = "THIRD-PARTY-NOTICES.md"
_VENDOR = "src/spark/web/static/vendor"
BUNDLED_ASSETS: dict[str, dict[str, object]] = {
    "Inter": {
        "licence": "OFL-1.1",
        "licence_file": "src/spark/web/static/fonts/Inter-OFL.txt",
        "files": [
            "src/spark/web/static/fonts/inter-latin-400.woff2",
            "src/spark/web/static/fonts/inter-latin-500.woff2",
            "src/spark/web/static/fonts/inter-latin-600.woff2",
            "src/spark/web/static/fonts/inter-latin-700.woff2",
        ],
    },
    # The document preview's renderers (CS38): served from SPARK itself, never a CDN.
    "PDF.js": {
        "version": "4.10.38",
        "source": "npm pdfjs-dist@4.10.38, build/",
        "licence": "Apache-2.0",
        "licence_file": f"{_VENDOR}/pdfjs/LICENSE.txt",
        "files": [f"{_VENDOR}/pdfjs/pdf.min.mjs", f"{_VENDOR}/pdfjs/pdf.worker.min.mjs"],
        "sha256": {
            f"{_VENDOR}/pdfjs/pdf.min.mjs": (
                "27fc2a057a00f92a4334ad06e17dbd7259912954e9fb7f76400bcca5fd190a9c"
            ),
            f"{_VENDOR}/pdfjs/pdf.worker.min.mjs": (
                "1baa1844c89c80a5b2797c916e75ab29254be46d8e9cb53cb6364d7aad84be36"
            ),
        },
    },
    "docx-preview": {
        "version": "0.4.1",
        "source": "npm docx-preview@0.4.1, dist/",
        "licence": "Apache-2.0",
        "licence_file": f"{_VENDOR}/docx-preview/LICENSE.txt",
        "files": [f"{_VENDOR}/docx-preview/docx-preview.min.js"],
        "sha256": {
            f"{_VENDOR}/docx-preview/docx-preview.min.js": (
                "c4a133c65a112799e35b143c572dfff9e923a3bf39b5fd3b07a06aa0f8f530c2"
            ),
        },
    },
    "JSZip": {
        "version": "3.10.2",
        "source": "npm jszip@3.10.2, dist/",
        "licence": "MIT",
        "offered": "MIT OR GPL-3.0-or-later; SPARK takes MIT",
        "licence_file": f"{_VENDOR}/jszip/LICENSE.txt",
        "files": [f"{_VENDOR}/jszip/jszip.min.js"],
        "sha256": {
            f"{_VENDOR}/jszip/jszip.min.js": (
                "7f839b2d4688b845c105ebf5d2f9803075f91ea0fe72bdaac176c3a04dd3d2c1"
            ),
        },
    },
    "SheetJS Community Edition": {
        "version": "0.20.3",
        "source": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz, package/dist/",
        "licence": "Apache-2.0",
        "licence_file": f"{_VENDOR}/sheetjs/LICENSE.txt",
        "files": [f"{_VENDOR}/sheetjs/xlsx.full.min.js"],
        "sha256": {
            f"{_VENDOR}/sheetjs/xlsx.full.min.js": (
                "cc015130aa8521e7f088f88898eba949ccdcbfb38df0bd129b44b7273c3a6f41"
            ),
        },
    },
    "marked": {
        "version": "18.3.0",
        "source": "npm marked@18.3.0, lib/",
        "licence": "MIT",
        "licence_file": f"{_VENDOR}/marked/LICENSE.txt",
        "files": [f"{_VENDOR}/marked/marked.umd.js"],
        "sha256": {
            f"{_VENDOR}/marked/marked.umd.js": (
                "f8c7f128939415995e2219e8a84bec42fab27f5980d22e361339e6901da5eadf"
            ),
        },
    },
    "DOMPurify": {
        "version": "3.5.0",
        "source": "npm dompurify@3.5.0, dist/",
        "licence": "Apache-2.0",
        "offered": "MPL-2.0 OR Apache-2.0; SPARK takes Apache-2.0",
        "licence_file": f"{_VENDOR}/dompurify/LICENSE.txt",
        "files": [f"{_VENDOR}/dompurify/purify.min.js"],
        "sha256": {
            f"{_VENDOR}/dompurify/purify.min.js": (
                "f69a46022ab1f53e969c8ebbca98172c8995785a9b3be72ea4a12d25b1a1bf67"
            ),
        },
    },
}
_ALLOWED_ASSET_LICENCES = {"OFL-1.1", "MIT", "Apache-2.0", "BSD-3-Clause", "ISC"}

_STRONG_SPDX = re.compile(r"^(A?GPL|SSPL|EUPL|OSL)-", re.I)
_WEAK_SPDX = re.compile(r"^(LGPL|MPL|EPL|CDDL)-", re.I)
_PERMISSIVE_SPDX = re.compile(
    r"^(MIT(-0|-CMU)?|0?BSD(-\d-Clause)?|BSD-\d-Clause(-\w+)?|Apache-\d\.\d|ISC|Zlib|"
    r"PSF-\d\.\d|Python-\d\.\d|CNRI-Python|Unlicense|CC0-1\.0|BSL-1\.0|HPND|MIT-CMU)$",
    re.I,
)
_STRONG_TEXT = re.compile(
    r"\b(affero|AGPL|SSPL|server side public|EUPL|european union public|OSL|"
    r"open software licen[cs]e)\b|(?<!lesser )(?<!library )general public licen[cs]e|\bGPL",
    re.I,
)
_WEAK_TEXT = re.compile(
    r"lesser general public|library general public|\bLGPL|mozilla public|\bMPL|"
    r"eclipse public|\bEPL\b|\bCDDL\b",
    re.I,
)
_PERMISSIVE_TEXT = re.compile(
    r"\b(MIT|BSD|Apache|ISC|zlib|Python Software Foundation|PSF|public domain|"
    r"Unlicense|CC0|HPND|Boost)\b",
    re.I,
)


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    category: Category
    licence: str


def _spdx_term(term: str) -> Category:
    term = term.strip().strip("()").strip()
    term = re.split(r"\s+WITH\s+", term, flags=re.I)[0].strip()
    if _STRONG_SPDX.match(term) and not term.upper().startswith("LGPL"):
        return Category.STRONG
    if _WEAK_SPDX.match(term):
        return Category.WEAK
    if _PERMISSIVE_SPDX.match(term):
        return Category.PERMISSIVE
    return Category.UNKNOWN


def _spdx(expression: str) -> Category:
    """OR offers a choice (the least restrictive counts); AND requires all (the most)."""
    expression = expression.strip()
    # Evaluate parenthesised groups first.
    while "(" in expression:
        inner = re.search(r"\(([^()]*)\)", expression)
        if inner is None:
            break
        category = _spdx(inner.group(1))
        expression = (
            expression[: inner.start()] + f"__{category.name}__" + expression[inner.end() :]
        )
    alternatives = re.split(r"\s+OR\s+", expression, flags=re.I)
    results = []
    for alternative in alternatives:
        terms = re.split(r"\s+AND\s+", alternative, flags=re.I)
        cats = []
        for term in terms:
            marker = re.fullmatch(r"__(\w+)__", term.strip())
            cats.append(Category[marker.group(1)] if marker else _spdx_term(term))
        results.append(max(cats))
    return min(results)


def _text(text: str) -> Category:
    if _WEAK_TEXT.search(text):
        return Category.WEAK
    if _STRONG_TEXT.search(text):
        return Category.STRONG
    if _PERMISSIVE_TEXT.search(text):
        return Category.PERMISSIVE
    return Category.UNKNOWN


def classify(expression: str | None, classifiers: list[str], text: str | None) -> Category:
    """Category from the SPDX expression, else the licence classifiers, else the text."""
    if expression and expression.strip():
        return _spdx(expression)
    names = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")]
    names = [n for n in names if n not in ("OSI Approved", "Other/Proprietary License")]
    if names:
        # Several classifiers list the licences on offer: the least restrictive applies.
        return min(_text(n) for n in names)
    if text and text.strip():
        return _text(text.strip().splitlines()[0])
    return Category.UNKNOWN


def check(packages: Iterable[Package], exceptions: Mapping[str, str]) -> list[str]:
    """Policy violations, one line each (empty when the closure complies)."""
    allowed = {canonicalize_name(name) for name in exceptions}
    problems: list[str] = []
    for pkg in packages:
        name = canonicalize_name(pkg.name)
        if pkg.category is Category.STRONG:
            problems.append(
                f"{pkg.name} {pkg.version}: strong copyleft ({pkg.licence}) is not allowed"
            )
        elif pkg.category is Category.WEAK and name not in allowed:
            problems.append(
                f"{pkg.name} {pkg.version}: weak copyleft ({pkg.licence}) needs a named, "
                "reviewed exception"
            )
        elif pkg.category is Category.UNKNOWN and name not in allowed:
            problems.append(f"{pkg.name} {pkg.version}: licence unknown ({pkg.licence or 'none'})")
    return problems


def _describe(meta: PackageMetadata, classifiers: list[str]) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    names = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")]
    if names:
        return "; ".join(names)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else ""


def runtime_closure(root: str) -> list[Package]:
    """Every installed distribution the root needs at run time (no extras)."""
    seen: dict[str, Package] = {}
    visited: set[tuple[str, frozenset[str]]] = set()
    queue: list[tuple[str, frozenset[str]]] = [(root, frozenset())]
    root_key = canonicalize_name(root)
    while queue:
        name, extras = queue.pop()
        key = canonicalize_name(name)
        if (key, extras) in visited:
            continue
        visited.add((key, extras))
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            raise SystemExit(f"{name} is required but not installed") from None
        if key not in seen:
            meta = dist.metadata
            classifiers = meta.get_all("Classifier") or []
            category = classify(meta.get("License-Expression"), classifiers, meta.get("License"))
            seen[key] = Package(dist.name, dist.version, category, _describe(meta, classifiers))
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None and not any(
                req.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                continue
            queue.append((req.name, frozenset(req.extras)))
    seen.pop(root_key, None)  # SPARK's own licence is not a dependency's
    return sorted(seen.values(), key=lambda p: canonicalize_name(p.name))


def check_bundled_assets(
    assets: Mapping[str, Mapping[str, object]] = BUNDLED_ASSETS, root: Path = ROOT
) -> list[str]:
    """Problems with the third-party files shipped in the wheel (empty when compliant)."""
    problems: list[str] = []
    notices = root / NOTICES
    notice_text = notices.read_text(encoding="utf-8") if notices.is_file() else ""
    if not notice_text:
        problems.append(f"{NOTICES} is missing or empty")
    for name, asset in assets.items():
        licence = str(asset["licence"])
        if licence not in _ALLOWED_ASSET_LICENCES:
            problems.append(f"{name}: licence {licence} is not an allowed bundled-asset licence")
        licence_file = root / str(asset["licence_file"])
        if not licence_file.is_file() or not licence_file.read_text(encoding="utf-8").strip():
            problems.append(f"{name}: licence text {asset['licence_file']} is missing")
        digests: Mapping[str, str] = asset.get("sha256", {})  # type: ignore[assignment]
        version = asset.get("version")
        for relative in asset["files"]:  # type: ignore[attr-defined]
            path = root / relative
            if not path.is_file():
                problems.append(f"{name}: bundled file {relative} is missing")
                continue
            expected = digests.get(relative)
            if version is not None and expected is None:
                problems.append(f"{name}: bundled file {relative} has no pinned checksum")
            elif expected is not None:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected:
                    problems.append(
                        f"{name}: bundled file {relative} does not match its pinned checksum"
                    )
        if name not in notice_text or licence not in notice_text:
            problems.append(f"{name}: {NOTICES} does not name it and its licence {licence}")
        elif version is not None and f"{name} {version}" not in notice_text:
            problems.append(f"{name}: {NOTICES} does not name its version {version}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default="cognisn-spark", help="distribution to audit")
    args = parser.parse_args(argv)

    packages = runtime_closure(args.root)
    problems = check(packages, EXCEPTIONS)
    problems += check_bundled_assets()
    width = max(len(p.name) for p in packages)
    for pkg in packages:
        flag = ""
        if canonicalize_name(pkg.name) in {canonicalize_name(n) for n in EXCEPTIONS}:
            flag = "  [named exception]"
        print(f"{pkg.name:<{width}}  {pkg.category.name.lower():<10}  {pkg.licence}{flag}")
    print(f"\n{len(packages)} runtime dependencies audited")
    print(f"{len(BUNDLED_ASSETS)} bundled third-party assets audited: {', '.join(BUNDLED_ASSETS)}")
    if problems:
        print("\nLicence policy violations:")
        for line in problems:
            print(f"  {line}")
        return 1
    print("No licence policy violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
