"""Content-addressed cache for synthesised audio.

A cache hit costs no API call and bills no characters, which matters because
ElevenLabs meters per character and re-reading a debate transcript would
otherwise be charged every time.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioCache:
    """MP3 blobs keyed by a hash of (text, voice, model), evicted LRU."""

    def __init__(self, root: Path, max_mb: int = 200) -> None:
        self._root = Path(root)
        self._max_bytes = max(0, int(max_mb)) * 1024 * 1024
        self._root.mkdir(parents=True, exist_ok=True)

    def key(self, text: str, voice_id: str, model_id: str) -> str:
        digest = hashlib.sha256()
        # The separator prevents ("ab", "c") colliding with ("a", "bc").
        digest.update(f"{text}\x00{voice_id}\x00{model_id}".encode())
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self._root / f"{key}.mp3"

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            audio = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path, None)  # mark as recently used, for eviction order
        except OSError:
            pass  # a failed touch must not fail the read
        return audio

    def put(self, key: str, audio: bytes) -> None:
        try:
            self._path(key).write_bytes(audio)
        except OSError:
            logger.warning("Could not write voice cache entry", exc_info=True)
            return
        self._evict()

    def _evict(self) -> None:
        if self._max_bytes <= 0:
            return
        try:
            entries = [(p.stat().st_mtime, p.stat().st_size, p) for p in self._root.glob("*.mp3")]
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        if total <= self._max_bytes:
            return
        for _mtime, size, path in sorted(entries):  # oldest mtime first
            if total <= self._max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
