"""Voice synthesis orchestration: cache, cap, API, billing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spark.database import voice_usage
from spark.voice.cache import AudioCache
from spark.voice.elevenlabs import DEFAULT_MODEL, ElevenLabsClient
from spark.voice.errors import VoiceUnavailable
from spark.voice.usage import over_cap

logger = logging.getLogger(__name__)

VALID_MODES = ("listen_along", "immersive", "listen_only")


@dataclass
class VoiceConfig:
    enabled: bool = False
    api_key: str = ""
    model_id: str = DEFAULT_MODEL
    default_voice_id: str = ""
    monthly_character_cap: int = 0
    cache_enabled: bool = True
    cache_max_mb: int = 200
    interaction_mode: str = "listen_along"


def load_config(ctx: Any) -> VoiceConfig:
    """Read the voice settings. Any failure yields a disabled config."""
    try:
        settings = ctx.settings
        enabled = settings.get("voice.engine", "browser", cast=str) == "elevenlabs"
        key = ""
        if getattr(ctx, "secrets", None):
            key = ctx.secrets.get("elevenlabs_api_key") or ""
        if not key:
            key = settings.get("voice.elevenlabs.api_key", "", cast=str) or ""
        mode = settings.get("voice.interaction_mode", "listen_along", cast=str)
        return VoiceConfig(
            enabled=bool(enabled),
            api_key=key,
            model_id=settings.get("voice.elevenlabs.model_id", DEFAULT_MODEL, cast=str),
            default_voice_id=settings.get(
                "voice.elevenlabs.default_voice_id", "", cast=str
            ),
            monthly_character_cap=int(
                settings.get("voice.elevenlabs.monthly_character_cap", 0, cast=int) or 0
            ),
            cache_enabled=bool(
                settings.get("voice.elevenlabs.cache_enabled", True, cast=bool)
            ),
            cache_max_mb=int(
                settings.get("voice.elevenlabs.cache_max_mb", 200, cast=int) or 200
            ),
            interaction_mode=mode if mode in VALID_MODES else "listen_along",
        )
    except Exception:  # noqa: BLE001 - a broken config must not break the page
        logger.warning("Could not load voice config; voice disabled", exc_info=True)
        return VoiceConfig()


class VoiceEngine:
    """Turns text into audio, or explains why it cannot."""

    def __init__(
        self,
        config: VoiceConfig,
        db: Any,
        cache_root: Path,
        *,
        client: Any = None,
    ) -> None:
        self.config = config
        self._db = db
        self._client = client or ElevenLabsClient(config.api_key, config.model_id)
        self._cache = AudioCache(Path(cache_root), config.cache_max_mb)

    def available(self) -> bool:
        return bool(self.config.enabled and self.config.api_key)

    def voices(self) -> list[dict]:
        if not self.available():
            return []
        return self._client.list_voices()

    def synthesise(
        self,
        text: str,
        voice_id: str | None = None,
        *,
        user_guid: str,
        conversation_id: int | None = None,
    ) -> bytes:
        if not self.available():
            raise VoiceUnavailable("disabled")

        voice = voice_id or self.config.default_voice_id
        if not voice:
            raise VoiceUnavailable("disabled")  # nothing to speak with

        key = self._cache.key(text, voice, self.config.model_id)

        # Cache first: a hit costs nothing, so it is served even over the cap.
        if self.config.cache_enabled:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        if over_cap(self._db, user_guid, self.config.monthly_character_cap):
            raise VoiceUnavailable("cap")

        audio = self._client.synthesise(text, voice)  # raises VoiceUnavailable

        voice_usage.record(
            self._db,
            user_guid,
            len(text),
            conversation_id=conversation_id,
            model_id=self.config.model_id,
        )
        if self.config.cache_enabled:
            self._cache.put(key, audio)
        return audio
