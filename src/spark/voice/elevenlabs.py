"""ElevenLabs REST client.

Uses httpx directly rather than the elevenlabs SDK: httpx is already a core
dependency and we need only two endpoints, which matters because Spark ships
as a PyApp binary.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from spark.voice.errors import VoiceUnavailable, classify_error

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_flash_v2_5"
OUTPUT_FORMAT = "mp3_44100_128"

# Retrying an exhausted quota or a rejected key is pointless; only transient
# failures are worth a second attempt.
_RETRYABLE = frozenset({"rate_limited", "server"})


class ElevenLabsClient:
    """Synthesises speech and lists voices. Raises VoiceUnavailable on failure."""

    def __init__(
        self,
        api_key: str,
        model_id: str = DEFAULT_MODEL,
        *,
        http: Any = None,
        timeout: float = 10.0,
        retry_delay: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id or DEFAULT_MODEL
        self._timeout = timeout
        self._http = http
        self._retry_delay = retry_delay
        self._voices: list[dict] | None = None

    # -- internals ---------------------------------------------------------

    def _client(self) -> Any:
        if self._http is not None:
            return self._http
        import httpx

        return httpx.Client(timeout=self._timeout)

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._api_key, "Accept": "audio/mpeg"}

    @staticmethod
    def _body(response: Any) -> Any:
        try:
            return response.json()
        except Exception:  # noqa: BLE001 - a non-JSON error body is still a failure
            return None

    # -- public ------------------------------------------------------------

    def synthesise(self, text: str, voice_id: str) -> bytes:
        """Render text as MP3 audio. Retries once on transient failures."""
        url = f"{API_BASE}/v1/text-to-speech/{voice_id}/stream"
        payload = {
            "text": text,
            "model_id": self._model_id,
            # 'speed' is deliberately omitted: the docs give conflicting ranges.
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0,  # non-zero adds latency for no benefit here
                "use_speaker_boost": True,
            },
        }
        params = {"output_format": OUTPUT_FORMAT}

        last_reason = "server"
        for attempt in (1, 2):
            try:
                resp = self._client().post(
                    url, headers=self._headers(), params=params, json=payload
                )
            except Exception as e:  # noqa: BLE001 - any transport failure
                logger.warning("ElevenLabs request failed: %s", e)
                raise VoiceUnavailable("network") from e

            if resp.status_code == 200:
                return resp.content

            last_reason = classify_error(resp.status_code, self._body(resp))
            if last_reason not in _RETRYABLE or attempt == 2:
                raise VoiceUnavailable(last_reason)
            if self._retry_delay:
                time.sleep(self._retry_delay)

        raise VoiceUnavailable(last_reason)

    def list_voices(self) -> list[dict]:
        """Premade voices, cached for the process. Never raises: [] on failure."""
        if self._voices is not None:
            return self._voices
        try:
            resp = self._client().get(
                f"{API_BASE}/v2/voices",  # /v1/voices is deprecated
                headers={"xi-api-key": self._api_key},
                params={"category": "premade"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json() or {}
            self._voices = [
                {
                    "voice_id": v.get("voice_id", ""),
                    "name": v.get("name", ""),
                    "labels": v.get("labels", {}),
                    "preview_url": v.get("preview_url", ""),
                }
                for v in data.get("voices", [])
                if v.get("voice_id")
            ]
            return self._voices
        except Exception:  # noqa: BLE001 - an empty picker beats a broken page
            logger.warning("Could not list ElevenLabs voices", exc_info=True)
            return []
