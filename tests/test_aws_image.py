"""Static checks of the image's AWS pieces: the RDS bundle, the health check, the image check."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
CHECK_IMAGE = (ROOT / "deploy" / "check-image.sh").read_text(encoding="utf-8")
BUNDLE_PATH = "/etc/ssl/certs/rds-global-bundle.pem"


def test_the_image_carries_the_rds_bundle_at_the_fixed_path() -> None:
    assert re.search(rf"COPY --from=rds-ca\b.*\s{re.escape(BUNDLE_PATH)}\s*$", DOCKERFILE, re.M)
    assert "--chmod=0644" in DOCKERFILE.split("FROM ${PYTHON_IMAGE}\n\nARG VERSION", 1)[1]


def test_the_bundle_is_fetched_from_aws_and_checked_against_a_pinned_checksum() -> None:
    stage = DOCKERFILE.split("AS rds-ca", 1)[1].split("# -- runtime", 1)[0]
    assert "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem" in stage
    assert re.search(r"ARG RDS_CA_BUNDLE_SHA256=[0-9a-f]{64}\n", stage)
    assert "hashlib.sha256" in stage
    assert "raise SystemExit" in stage  # a mismatch fails the build
    assert "PRIVATE KEY" in stage  # a bundle that is not certificate-only fails it too


def test_the_final_stage_does_not_gain_a_downloader() -> None:
    final = DOCKERFILE.split("# -- runtime", 1)[1]
    assert "curl" not in final and "wget" not in final


def test_the_image_health_check_probes_health() -> None:
    health = DOCKERFILE.split("HEALTHCHECK", 1)[1].splitlines()[1]
    assert "/health'" in health
    assert "/loading/api/status" not in health


def test_the_image_check_verifies_the_bundle_and_the_health_check() -> None:
    assert BUNDLE_PATH in CHECK_IMAGE
    assert "BEGIN CERTIFICATE" in CHECK_IMAGE and "PRIVATE KEY" in CHECK_IMAGE
    assert "*/health*" in CHECK_IMAGE
