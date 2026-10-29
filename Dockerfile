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
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN pip install --upgrade pip \
    && pip install torch --index-url "${TORCH_INDEX_URL}"

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && pip check

# Fail the build if the runtime closure breaks the licence policy.
COPY scripts/licence_audit.py ./scripts/licence_audit.py
RUN python scripts/licence_audit.py

# No installers in the runtime environment.
RUN pip uninstall --yes pip setuptools wheel

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
    && mkdir -p /var/lib/spark /var/cache/spark/huggingface \
    && chown -R 10001:10001 /var/lib/spark /var/cache/spark

COPY --from=builder /opt/venv /opt/venv

# Configuration, data, logs and secrets live under /var/lib/spark (one volume,
# shared by the web and worker containers); the embedding model cache has its own.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/spark \
    XDG_CONFIG_HOME=/var/lib/spark/config \
    XDG_DATA_HOME=/var/lib/spark/data \
    XDG_STATE_HOME=/var/lib/spark/state \
    HF_HOME=/var/cache/spark/huggingface \
    SPARK__INTERFACE__HOST=0.0.0.0 \
    SPARK__INTERFACE__PORT=8000 \
    SPARK__INTERFACE__OPEN_BROWSER=false \
    SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=false

USER 10001:10001
WORKDIR /var/lib/spark
VOLUME ["/var/lib/spark", "/var/cache/spark/huggingface"]
EXPOSE 8000

# For the web role; the worker and migrate services disable it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SPARK__INTERFACE__PORT', '8000') + '/loading/api/status', timeout=4)"]

ENTRYPOINT ["spark"]
CMD ["web"]
