# Spark container image: one image, two roles (`spark web`, `spark worker`), plus
# `spark migrate`. See docs/container.md.
#
#   docker build -t spark:dev .
#
# The builder stage installs Spark and its dependencies into a virtual environment
# and runs the licence audit against exactly that environment; the final stage
# copies the environment onto a clean base with no compilers and no pip.

ARG PYTHON_IMAGE=python:3.12-slim

# -- builder ------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# The CPU-only PyTorch wheel: sentence-transformers needs torch, and the default
# Linux wheel pulls in the NVIDIA CUDA runtime (proprietary, and gigabytes).
# pip installs torch first from the CPU index, so `pip install .` keeps it (the
# per-installer route in docs/getting-started.md); the build then fails if any
# nvidia-* package is installed.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN pip install --upgrade pip \
    && pip install torch --index-url "${TORCH_INDEX_URL}"

WORKDIR /build
COPY pyproject.toml README.md LICENSE THIRD-PARTY-NOTICES.md ./
COPY src ./src
RUN pip install . \
    && pip check

# Fail the build if a CUDA package slipped in, or the runtime closure breaks the
# licence policy.
COPY scripts/check_no_nvidia.py ./scripts/check_no_nvidia.py
COPY scripts/licence_audit.py ./scripts/licence_audit.py
RUN python scripts/check_no_nvidia.py \
    && python scripts/licence_audit.py

# No installers in the runtime environment.
RUN pip uninstall --yes pip setuptools wheel

# -- RDS certificate authority bundle -----------------------------------------
# AWS's public certificate bundle for RDS, fetched at build time and checked
# against a pinned SHA-256 so a changed or tampered download fails the build.
# AWS republishes the bundle when it adds a certificate authority: refresh
# RDS_CA_BUNDLE_SHA256 (build argument) after checking the new bundle.
FROM ${PYTHON_IMAGE} AS rds-ca
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
RUN python - <<'PY'
import hashlib
import os
import urllib.request

data = urllib.request.urlopen(os.environ["RDS_CA_BUNDLE_URL"], timeout=60).read()
digest = hashlib.sha256(data).hexdigest()
expected = os.environ["RDS_CA_BUNDLE_SHA256"]
if digest != expected:
    raise SystemExit(f"RDS certificate bundle checksum mismatch: {digest} != {expected}")
if data.count(b"BEGIN CERTIFICATE") < 1 or b"PRIVATE KEY" in data:
    raise SystemExit("RDS certificate bundle is not a certificate-only PEM file")
with open("/rds-global-bundle.pem", "wb") as fh:
    fh.write(data)
os.chmod("/rds-global-bundle.pem", 0o644)
PY

# -- runtime ------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ARG VERSION=dev
LABEL org.opencontainers.image.title="Spark" \
      org.opencontainers.image.description="Secure Personal AI Research Kit: web and worker roles" \
      org.opencontainers.image.source="https://github.com/Cognisn/spark" \
      org.opencontainers.image.version="${VERSION}"

# libmagic for python-magic (file type detection). Then remove the base image's
# own pip, so the final stage carries no installer or build tool.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
              /usr/local/lib/python3.12/site-packages/pip-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# A fixed, unprivileged user (numeric, so orchestrators can verify it is not root).
RUN groupadd --system --gid 10001 spark \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/spark \
               --shell /usr/sbin/nologin spark \
    && mkdir -p /var/lib/spark /var/lib/spark-files /var/cache/spark/huggingface \
    && chown -R 10001:10001 /var/lib/spark /var/lib/spark-files /var/cache/spark

COPY --from=builder /opt/venv /opt/venv

# The third-party notices (the Inter font's licence text also ships in the package,
# beside the fonts), readable by anyone who has the image.
COPY --from=builder --chmod=0644 /build/THIRD-PARTY-NOTICES.md /usr/share/doc/spark/THIRD-PARTY-NOTICES.md

# The RDS certificate authority bundle (public certificates only), at the fixed
# path tasks name in PGSSLROOTCERT so sslmode=verify-full works against RDS with
# no sslrootcert in the database URL. Readable by the unprivileged user.
COPY --from=rds-ca --chmod=0644 /rds-global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem

# Configuration, data, logs and secrets live under /var/lib/spark (one volume,
# shared by the web and worker containers); users' file areas live under
# /var/lib/spark-files (server mode sets files.root to it); the embedding model
# cache, and tiktoken's encoding cache beside it, have their own. Nothing else is
# written but /tmp, so the root filesystem can be mounted read-only
# (docs/deployment/container-hardening.md).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/spark \
    XDG_CONFIG_HOME=/var/lib/spark/config \
    XDG_DATA_HOME=/var/lib/spark/data \
    XDG_STATE_HOME=/var/lib/spark/state \
    HF_HOME=/var/cache/spark/huggingface \
    TIKTOKEN_CACHE_DIR=/var/cache/spark/huggingface/tiktoken \
    SPARK__INTERFACE__HOST=0.0.0.0 \
    SPARK__INTERFACE__PORT=8000 \
    SPARK__INTERFACE__OPEN_BROWSER=false \
    SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=false

USER 10001:10001
WORKDIR /var/lib/spark
# /tmp is declared too, so a platform that copies a volume's ownership and mode
# from the image (ECS on Fargate, for a bind-mount task volume) gives the
# unprivileged user a writable /tmp under a read-only root. Compose mounts a
# tmpfs over it instead.
VOLUME ["/var/lib/spark", "/var/lib/spark-files", "/var/cache/spark/huggingface", "/tmp"]
EXPOSE 8000

# For the web role; the worker and migrate services disable it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SPARK__INTERFACE__PORT', '8000') + '/health', timeout=4)"]

ENTRYPOINT ["spark"]
CMD ["web"]
