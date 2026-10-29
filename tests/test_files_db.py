"""User file metadata and the per-user usage counter (SQLite and PostgreSQL)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spark.database import files

ALICE = "user-alice-0001"
BOB = "user-bob-0002"


def _new(key: str, size: int, **kw: Any) -> files.NewUserFile:
    return files.NewUserFile(key=key, filename=key.rsplit("/", 1)[-1], size=size, **kw)


class TestRecord:
    async def test_record_list_get(self, spark_db: Any) -> None:
        fid = await files.record_user_file(
            spark_db, ALICE, _new("a/report.docx", 10, mime_type="x/y"), quota_bytes=None
        )
        rows = await files.list_user_files(spark_db, ALICE)
        assert [r["storage_key"] for r in rows] == ["a/report.docx"]
        row = await files.get_user_file(spark_db, ALICE, fid)
        assert row is not None
        assert row["filename"] == "report.docx"
        assert row["size_bytes"] == 10
        assert row["mime_type"] == "x/y"
        assert row["source"] == "upload"
        assert await files.get_usage(spark_db, ALICE) == 10

    async def test_another_users_file_is_not_found(self, spark_db: Any) -> None:
        fid = await files.record_user_file(spark_db, ALICE, _new("a.txt", 1), quota_bytes=None)
        assert await files.get_user_file(spark_db, BOB, fid) is None
        assert await files.list_user_files(spark_db, BOB) == []
        assert await files.delete_user_file(spark_db, BOB, fid) is None
        assert await files.get_user_file(spark_db, ALICE, fid) is not None
        assert await files.get_usage(spark_db, ALICE) == 1

    async def test_replacing_a_key_charges_only_the_difference(self, spark_db: Any) -> None:
        first = await files.record_user_file(spark_db, ALICE, _new("a.txt", 60), quota_bytes=100)
        second = await files.record_user_file(spark_db, ALICE, _new("a.txt", 90), quota_bytes=100)
        assert first == second
        assert await files.get_usage(spark_db, ALICE) == 90
        assert len(await files.list_user_files(spark_db, ALICE)) == 1

    async def test_find_by_key(self, spark_db: Any) -> None:
        fid = await files.record_user_file(spark_db, ALICE, _new("k/x.pdf", 3), quota_bytes=None)
        row = await files.get_user_file_by_key(spark_db, ALICE, "k/x.pdf")
        assert row is not None and row["id"] == fid
        assert await files.get_user_file_by_key(spark_db, BOB, "k/x.pdf") is None

    async def test_delete_releases_usage(self, spark_db: Any) -> None:
        fid = await files.record_user_file(spark_db, ALICE, _new("a.txt", 40), quota_bytes=None)
        row = await files.delete_user_file(spark_db, ALICE, fid)
        assert row is not None and row["storage_key"] == "a.txt"
        assert await files.get_usage(spark_db, ALICE) == 0
        assert await files.delete_user_file(spark_db, ALICE, fid) is None

    async def test_batch_is_all_or_nothing(self, spark_db: Any) -> None:
        with pytest.raises(files.QuotaExceeded):
            await files.record_user_files(
                spark_db, ALICE, [_new("a", 60), _new("b", 60)], quota_bytes=100
            )
        assert await files.list_user_files(spark_db, ALICE) == []
        assert await files.get_usage(spark_db, ALICE) == 0


class TestQuota:
    async def test_over_quota_is_refused_with_a_clear_message(self, spark_db: Any) -> None:
        await files.record_user_file(spark_db, ALICE, _new("a", 80), quota_bytes=100)
        with pytest.raises(files.QuotaExceeded) as info:
            await files.record_user_file(spark_db, ALICE, _new("b", 21), quota_bytes=100)
        assert "quota" in str(info.value).lower()
        assert info.value.used == 80 and info.value.requested == 21 and info.value.quota == 100
        assert await files.get_usage(spark_db, ALICE) == 80
        assert await files.get_user_file_by_key(spark_db, ALICE, "b") is None

    async def test_exactly_at_quota_is_accepted(self, spark_db: Any) -> None:
        await files.record_user_file(spark_db, ALICE, _new("a", 100), quota_bytes=100)
        assert await files.get_usage(spark_db, ALICE) == 100

    async def test_quota_is_per_user(self, spark_db: Any) -> None:
        await files.record_user_file(spark_db, ALICE, _new("a", 100), quota_bytes=100)
        await files.record_user_file(spark_db, BOB, _new("a", 100), quota_bytes=100)
        assert await files.get_usage(spark_db, BOB) == 100

    async def test_parallel_records_never_exceed_the_quota(self, spark_db: Any) -> None:
        """Twenty concurrent 10-byte records against a 55-byte quota: exactly five fit."""

        async def one(i: int) -> bool:
            try:
                await files.record_user_file(spark_db, ALICE, _new(f"f{i}", 10), quota_bytes=55)
            except files.QuotaExceeded:
                return False
            return True

        results = await asyncio.gather(*(one(i) for i in range(20)))
        assert sum(results) == 5
        assert await files.get_usage(spark_db, ALICE) == 50
        rows = await files.list_user_files(spark_db, ALICE)
        assert sum(r["size_bytes"] for r in rows) == 50

    async def test_parallel_replacements_of_one_key_keep_usage_exact(self, spark_db: Any) -> None:
        await files.record_user_file(spark_db, ALICE, _new("same", 1), quota_bytes=None)

        async def one(size: int) -> None:
            await files.record_user_file(spark_db, ALICE, _new("same", size), quota_bytes=None)

        await asyncio.gather(*(one(s) for s in range(2, 22)))
        row = await files.get_user_file_by_key(spark_db, ALICE, "same")
        assert row is not None
        assert await files.get_usage(spark_db, ALICE) == row["size_bytes"]
