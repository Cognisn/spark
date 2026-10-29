"""The GHCR tag rule (deploy/image_tags.py): 0.3.0rcN and 0.3.0 only, matching, never latest."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "image_tags.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("image_tags", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["image_tags"] = module
    spec.loader.exec_module(module)
    return module


image_tags = _load()
IMAGE = "ghcr.io/Cognisn/spark"


class TestTags:
    @pytest.mark.parametrize("version", ["0.3.0", "0.3.0rc1", "0.3.0rc12", "1.0.0"])
    def test_final_and_release_candidate_versions_get_one_tag(self, version: str) -> None:
        assert image_tags.tags(IMAGE, version, f"refs/tags/v{version}") == [
            f"ghcr.io/cognisn/spark:{version}"
        ]

    def test_a_manual_dispatch_from_a_branch_uses_the_version(self) -> None:
        assert image_tags.tags(IMAGE, "0.3.0rc2", "refs/heads/release/v0.3.0") == [
            "ghcr.io/cognisn/spark:0.3.0rc2"
        ]

    @pytest.mark.parametrize("version", ["0.3.0a1", "0.3.0b1", "0.3.0.dev1", "0.3.0rc1.post1"])
    def test_alpha_beta_and_other_versions_are_refused(self, version: str) -> None:
        with pytest.raises(image_tags.TagRefused, match=version):
            image_tags.tags(IMAGE, version, "refs/heads/release/v0.3.0")

    def test_a_tag_that_does_not_match_the_version_is_refused(self) -> None:
        with pytest.raises(image_tags.TagRefused, match="v0.3.0rc2"):
            image_tags.tags(IMAGE, "0.3.0rc1", "refs/tags/v0.3.0rc2")

    @pytest.mark.parametrize("version", ["latest", "Latest", "stable"])
    def test_latest_is_never_produced(self, version: str) -> None:
        with pytest.raises(image_tags.TagRefused):
            image_tags.tags(IMAGE, version, "refs/heads/main")

    def test_no_tag_is_ever_latest(self) -> None:
        for version in ("0.3.0", "0.3.0rc1"):
            for tag in image_tags.tags(IMAGE, version, f"refs/tags/v{version}"):
                assert not tag.endswith(":latest")


class TestMain:
    def test_prints_the_tags(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        version_file = tmp_path / "_version.txt"
        version_file.write_text("0.3.0rc1\n")
        code = image_tags.main(
            ["--image", IMAGE, "--version-file", str(version_file), "--ref", "refs/tags/v0.3.0rc1"]
        )
        assert code == 0
        assert capsys.readouterr().out.strip() == "ghcr.io/cognisn/spark:0.3.0rc1"

    def test_refuses_with_status_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        version_file = tmp_path / "_version.txt"
        version_file.write_text("0.3.0a1\n")
        code = image_tags.main(
            ["--image", IMAGE, "--version-file", str(version_file), "--ref", "refs/heads/x"]
        )
        assert code == 1
        assert "0.3.0a1" in capsys.readouterr().err
