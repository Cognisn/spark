"""Content-addressed audio cache with LRU eviction."""

from __future__ import annotations

import time

from spark.voice.cache import AudioCache


class TestKey:
    def test_stable_for_same_inputs(self, tmp_path) -> None:
        c = AudioCache(tmp_path)
        assert c.key("hello", "v1", "m1") == c.key("hello", "v1", "m1")

    def test_varies_with_text_voice_and_model(self, tmp_path) -> None:
        c = AudioCache(tmp_path)
        base = c.key("hello", "v1", "m1")
        assert c.key("HELLO", "v1", "m1") != base
        assert c.key("hello", "v2", "m1") != base  # same words, different voice
        assert c.key("hello", "v1", "m2") != base  # same voice, different model


class TestRoundTrip:
    def test_miss_then_hit(self, tmp_path) -> None:
        c = AudioCache(tmp_path)
        k = c.key("hello", "v1", "m1")
        assert c.get(k) is None
        c.put(k, b"audio-bytes")
        assert c.get(k) == b"audio-bytes"

    def test_get_is_safe_on_corrupt_entry(self, tmp_path) -> None:
        c = AudioCache(tmp_path)
        k = c.key("hello", "v1", "m1")
        c.put(k, b"x")
        (tmp_path / f"{k}.mp3").unlink()
        assert c.get(k) is None


class TestEviction:
    def test_evicts_oldest_until_under_cap(self, tmp_path) -> None:
        c = AudioCache(tmp_path, max_mb=1)
        blob = b"x" * (400 * 1024)  # 400 KB each; three exceed the 1 MB cap

        c.put("aaa", blob)
        time.sleep(0.01)
        c.put("bbb", blob)
        time.sleep(0.01)
        c.put("ccc", blob)

        assert c.get("aaa") is None  # oldest, evicted
        assert c.get("ccc") == blob  # newest, retained

    def test_get_refreshes_recency(self, tmp_path) -> None:
        c = AudioCache(tmp_path, max_mb=1)
        blob = b"x" * (400 * 1024)

        c.put("aaa", blob)
        time.sleep(0.01)
        c.put("bbb", blob)
        time.sleep(0.01)
        c.get("aaa")  # touch the oldest — it is now the newest
        time.sleep(0.01)
        c.put("ccc", blob)  # forces an eviction

        assert c.get("aaa") == blob  # survived because it was used
        assert c.get("bbb") is None  # now the least recently used
