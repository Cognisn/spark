"""The image tags a GHCR push may use, for the container workflow.

Rules (0.3.0 ruling A18): a pushed image is tagged with the version in
``src/spark/_version.txt`` only, and only when that version is final
(``0.3.0``) or a release candidate (``0.3.0rc1``); alpha, beta, development and
post releases are refused. A push from a ``v*`` tag needs the tag to name the
same version. ``latest`` is never produced.

    python deploy/image_tags.py --image ghcr.io/cognisn/spark \\
        --version-file src/spark/_version.txt --ref "$GITHUB_REF"

prints one tag per line, or exits with status 1 and the reason.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = ["TagRefused", "main", "tags"]

_PUSHABLE = re.compile(r"^\d+\.\d+\.\d+(rc\d+)?$")
_TAG_REF = "refs/tags/"


class TagRefused(ValueError):
    """The version or ref may not be pushed."""


def tags(image: str, version: str, ref: str) -> list[str]:
    """The tags for ``image`` at ``version`` pushed from ``ref``."""
    if not _PUSHABLE.match(version):
        raise TagRefused(
            f"version {version!r} is not pushable: only final versions (0.3.0) and "
            "release candidates (0.3.0rc1) are published"
        )
    if ref.startswith(_TAG_REF):
        tag = ref[len(_TAG_REF) :]
        if tag != f"v{version}":
            raise TagRefused(f"git tag {tag!r} does not match the version {version!r}")
    return [f"{image.lower()}:{version}"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--version-file", required=True, type=Path)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    version = args.version_file.read_text(encoding="utf-8").strip()
    try:
        result = tags(args.image, version, args.ref)
    except TagRefused as exc:
        print(f"image_tags: {exc}", file=sys.stderr)
        return 1
    print("\n".join(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
