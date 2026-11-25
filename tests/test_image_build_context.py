"""The image build context holds every file the Dockerfile's licence audit needs.

``.dockerignore`` excludes everything and allows files one by one, so a file the audit reads
(the notices file at the audit's root) that is not allowed fails the image build only at the
wave gate. These checks fail on the host instead.
"""

from __future__ import annotations

import importlib.util
import posixpath
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
NOTICES = "THIRD-PARTY-NOTICES.md"
RUNTIME_NOTICES = "/usr/share/doc/spark/THIRD-PARTY-NOTICES.md"


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "licence_audit", ROOT / "scripts/licence_audit.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _included(path: str) -> bool:
    """Whether the build context keeps ``path`` (the last matching pattern wins)."""
    kept = True
    for raw in DOCKERIGNORE.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        base = pattern.rstrip("/")
        if pattern == "*" or path == base or path.startswith(base + "/"):
            kept = negated
    return kept


def _copy_lines(stage_text: str) -> list[list[str]]:
    lines = re.sub(r"\\n", " ", stage_text).splitlines()
    copies = []
    for line in lines:
        if line.startswith("COPY ") and "--from=" not in line:
            words = [w for w in shlex.split(line)[1:] if not w.startswith("--")]
            copies.append(words)
    return copies


def _builder_stage() -> str:
    return DOCKERFILE.split("AS builder", 1)[1].split("# -- RDS certificate", 1)[0]


def test_every_file_the_dockerfile_copies_is_in_the_build_context() -> None:
    sources = [s for words in _copy_lines(DOCKERFILE) for s in words[:-1]]
    assert sources, "found no COPY lines"
    missing = [s.removeprefix("./") for s in sources if not _included(s.removeprefix("./"))]
    assert missing == []


def test_the_notices_file_the_audit_reads_is_in_the_build_context() -> None:
    audit = _load_audit()
    assert audit.NOTICES == NOTICES
    assert _included(NOTICES)


def test_the_builder_copies_the_notices_to_the_audits_root() -> None:
    # The audit's root is the parent of scripts/, so the notices must sit beside scripts/.
    builder = _builder_stage()
    copies = _copy_lines(builder)
    audit_copy = [c for c in copies if c[0] == "scripts/licence_audit.py"]
    assert audit_copy and audit_copy[0][-1].startswith("./scripts/")
    workdir = re.search(r"^WORKDIR (\S+)$", builder, re.M).group(1)
    root = posixpath.normpath(posixpath.join(workdir, audit_copy[0][-1], ".."))
    root = posixpath.dirname(root)
    notice_copy = [c for c in copies if NOTICES in c[:-1]]
    assert notice_copy, "the builder stage does not copy the notices file"
    dest = notice_copy[0][-1]
    assert posixpath.normpath(
        posixpath.join(workdir, dest, NOTICES)
        if dest.endswith("/")
        else posixpath.join(workdir, dest)
    ) == posixpath.join(root, NOTICES)
    # And it arrives before the audit runs.
    assert builder.index(NOTICES) < builder.index("python scripts/licence_audit.py")


def test_the_audit_is_not_skipped_or_weakened_in_the_image() -> None:
    assert "python scripts/licence_audit.py" in _builder_stage()
    for flag in ("--skip", "--no-assets", "|| true"):
        assert flag not in _builder_stage().split("licence_audit.py", 1)[1].split("\n\n", 1)[0]


def test_the_runtime_image_carries_the_notices_and_the_font_licence() -> None:
    runtime = DOCKERFILE.split("# -- runtime", 1)[1]
    assert re.search(rf"COPY --from=builder\b.*\s{re.escape(RUNTIME_NOTICES)}\s*$", runtime, re.M)
    # The font licence travels with the font, inside the installed package.
    assert (ROOT / "src/spark/web/static/fonts/Inter-OFL.txt").is_file()
