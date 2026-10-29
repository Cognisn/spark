"""Helpers for tests that run the web app with a real database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spark.web.server import attach_database


@contextmanager
def app_client(app: FastAPI, settings: Any) -> Iterator[TestClient]:
    """A TestClient whose lifespan has opened ``settings`` (auto-migrating).

    Inside the block ``app.state.database`` (async, for endpoints) and
    ``app.state.sync_db`` (the bridge, for mocked managers and seeding) are set.
    """
    attach_database(app, settings, True)
    with TestClient(app) as client:
        yield client
