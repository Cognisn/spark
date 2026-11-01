"""The image's no-NVIDIA check: the build fails if any nvidia-* package is installed (Q7)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_no_nvidia.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_no_nvidia", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_no_nvidia"] = module
    spec.loader.exec_module(module)
    return module


check = _load()


def test_cpu_only_environment_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(["torch", "sentence-transformers", "numpy"]) == 0
    assert "no NVIDIA packages" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name", ["nvidia-cublas-cu12", "nvidia_cudnn_cu12", "NVIDIA-NCCL-CU12", "nvidia.cufft"]
)
def test_any_nvidia_package_fails(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(["torch", name]) == 1
    assert "nvidia" in capsys.readouterr().err.lower()


def test_every_nvidia_package_is_named() -> None:
    found = check.nvidia_packages(["nvidia-cublas-cu12", "torch", "nvidia-nccl-cu12"])
    assert found == ["nvidia-cublas-cu12", "nvidia-nccl-cu12"]


def test_a_name_merely_mentioning_nvidia_is_not_flagged() -> None:
    assert check.nvidia_packages(["pynvml-tools", "not-nvidia"]) == []


def test_reads_the_installed_environment_by_default() -> None:
    names = check.installed()
    assert "pytest" in {n.lower() for n in names}
