"""Fail when any NVIDIA (CUDA) package is installed in this environment.

The container image installs the CPU-only PyTorch wheel (from the PyTorch CPU
index, before Spark), so no ``nvidia-*`` package may reach it; a resolver that
swapped in the default Linux wheel would pull in the CUDA runtime (proprietary,
and gigabytes). The Dockerfile runs this after installing Spark.

    python scripts/check_no_nvidia.py            # the running environment
    python scripts/check_no_nvidia.py NAME ...   # the given distribution names
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence
from importlib import metadata


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def nvidia_packages(names: Iterable[str]) -> list[str]:
    """The NVIDIA distributions among ``names``, sorted (``nvidia-*`` in any spelling)."""
    return sorted({_canonical(n) for n in names if _canonical(n).startswith("nvidia-")})


def installed() -> list[str]:
    """Every distribution installed in the running environment."""
    return [d.metadata["Name"] for d in metadata.distributions() if d.metadata["Name"]]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    found = nvidia_packages(args or installed())
    if found:
        print(
            "NVIDIA packages are installed (the image must use the CPU-only PyTorch wheel): "
            + ", ".join(found),
            file=sys.stderr,
        )
        return 1
    print("no NVIDIA packages installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
