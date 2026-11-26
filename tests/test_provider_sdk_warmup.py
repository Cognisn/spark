"""The provider SDKs conduit loads lazily are imported once, before the first test.

The first import of a provider SDK (anthropic alone is tens of seconds on a cold file
cache) must not be charged to whichever test first builds a provider, or that test can
exceed pytest-timeout's limit. tests/conftest.py imports them at collection finish,
outside every test's timeout.
"""

from __future__ import annotations

import importlib.util
import sys
import time

from tests.conftest import PROVIDER_SDKS, warm_provider_sdks


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def test_the_sdks_the_default_suite_reaches_are_listed() -> None:
    # The connections tests build anthropic and OpenAI-compatible providers through
    # conduit's registry; their SDKs are the ones that timed out (decision 233).
    assert "anthropic" in PROVIDER_SDKS
    assert "openai" in PROVIDER_SDKS


def test_every_installed_provider_sdk_is_imported_before_any_test() -> None:
    missing = [name for name in PROVIDER_SDKS if _installed(name) and name not in sys.modules]
    assert missing == []


def test_the_warm_up_costs_nothing_when_already_warm() -> None:
    started = time.perf_counter()
    warmed = warm_provider_sdks()
    assert time.perf_counter() - started < 0.5
    assert warmed == []
