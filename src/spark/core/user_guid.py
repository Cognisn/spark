"""User GUID management — generates and persists a unique user identifier."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

_USER_GUID_KEY = "spark_user_guid"
_cached_guid: str | None = None


def get_user_guid(ctx: object | None = None) -> str:
    """Get the persistent user GUID, generating one on first use.

    The GUID is stored in the secrets backend (OS keychain or encrypted file)
    and reused across all sessions.

    Falls back to 'default' if secrets are unavailable.
    """
    global _cached_guid

    if _cached_guid:
        return _cached_guid

    if ctx and hasattr(ctx, "secrets") and ctx.secrets:
        try:
            stored = ctx.secrets.get(_USER_GUID_KEY)
            if stored:
                _cached_guid = stored
                return stored

            # Generate a new GUID
            new_guid = str(uuid.uuid4())
            ctx.secrets.set(_USER_GUID_KEY, new_guid)
            _cached_guid = new_guid
            logger.info("Generated new user GUID: %s", new_guid[:8] + "...")
            return new_guid
        except Exception as e:
            logger.debug("Failed to access secrets for user GUID: %s", e)

    _cached_guid = "default"
    return _cached_guid


def reset_cache() -> None:
    """Reset the cached GUID (used in testing)."""
    global _cached_guid
    _cached_guid = None
