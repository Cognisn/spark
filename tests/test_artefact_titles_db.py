"""Artefact titles and kinds on user files (PC5), on SQLite and PostgreSQL.

Every file in a user's area has a title (shown in the artefacts panel) and a kind:
``document``, ``chart``, ``diagram``, ``preview`` or ``attachment``. A file recorded
without them takes its file name as its title and its kind from its source. The owner
renames their own file; in a conversation the owner and a contributor rename its
files, a viewer and a stranger cannot. The readable lookup reaches a member's file in a
conversation the reader may read, and nothing else of the owner's area.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark.database import conversation_members, conversations
from spark.database import files as files_db
from spark.database.scope import NotOwned, NotPermitted, Role

ADA = "user-ada-0001"
CORA = "user-cora-0002"
VIC = "user-vic-0003"
SAM = "user-sam-0004"


def _new(key: str, **kw: Any) -> files_db.NewUserFile:
    return files_db.NewUserFile(key=key, filename=key.rsplit("/", 1)[-1], size=5, **kw)


async def _record(db: Any, user: str, key: str, **kw: Any) -> int:
    return await files_db.record_user_file(db, user, _new(key, **kw), quota_bytes=None)


async def _shared(db: Any) -> int:
    cid = await conversations.create_conversation(db, "Shared", "stub/model", ADA)
    await conversation_members.share(db, ADA, cid, CORA, Role.CONTRIBUTOR)
    await conversation_members.share(db, ADA, cid, VIC, Role.VIEWER)
    return cid


class TestDefaults:
    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            ("upload", "attachment"),
            ("preview", "preview"),
            ("created", "document"),
            ("import", "attachment"),
        ],
    )
    async def test_title_is_the_file_name_and_kind_follows_the_source(
        self, spark_db: Any, source: str, kind: str
    ) -> None:
        fid = await _record(spark_db, ADA, f"a/{source}.pdf", source=source)
        row = await files_db.get_user_file(spark_db, ADA, fid)
        assert row is not None
        assert (row["title"], row["kind"]) == (f"{source}.pdf", kind)

    async def test_given_title_and_kind_are_kept(self, spark_db: Any) -> None:
        fid = await _record(
            spark_db,
            ADA,
            "charts/x/sales.chart.json",
            source="created",
            title="Sales",
            kind="chart",
        )
        row = await files_db.get_user_file(spark_db, ADA, fid)
        assert row is not None
        assert (row["title"], row["kind"]) == ("Sales", "chart")

    async def test_an_unknown_kind_is_refused(self, spark_db: Any) -> None:
        with pytest.raises(ValueError, match="document, chart, diagram, preview, attachment"):
            await _record(spark_db, ADA, "a.txt", kind="script")
        assert await files_db.list_user_files(spark_db, ADA) == []

    async def test_replacing_a_key_replaces_title_and_kind(self, spark_db: Any) -> None:
        first = await _record(spark_db, ADA, "a.json", title="One", kind="chart")
        second = await _record(spark_db, ADA, "a.json", title="Two", kind="diagram")
        assert first == second
        row = await files_db.get_user_file(spark_db, ADA, first)
        assert row is not None and (row["title"], row["kind"]) == ("Two", "diagram")


class TestRename:
    async def test_the_owner_renames_their_own_file(self, spark_db: Any) -> None:
        fid = await _record(spark_db, ADA, "a.pdf")
        row = await files_db.set_file_title(spark_db, ADA, fid, "Quarterly report")
        assert row is not None and row["title"] == "Quarterly report"
        assert (await files_db.get_file_title(spark_db, ADA, fid)) == "Quarterly report"

    async def test_another_users_file_is_not_renamed(self, spark_db: Any) -> None:
        fid = await _record(spark_db, ADA, "a.pdf")
        assert await files_db.set_file_title(spark_db, SAM, fid, "Mine now") is None
        assert (await files_db.get_file_title(spark_db, ADA, fid)) == "a.pdf"
        assert await files_db.get_file_title(spark_db, SAM, fid) is None

    async def test_a_title_is_cleaned_and_must_not_be_blank(self, spark_db: Any) -> None:
        fid = await _record(spark_db, ADA, "a.pdf")
        row = await files_db.set_file_title(spark_db, ADA, fid, "  Line\none\x00 <b>x</b> ")
        assert row is not None and row["title"] == "Line one ＜b＞x＜/b＞"
        with pytest.raises(ValueError, match="title"):
            await files_db.set_file_title(spark_db, ADA, fid, " \n\t")

    async def test_owner_and_contributor_rename_in_a_conversation(self, spark_db: Any) -> None:
        cid = await _shared(spark_db)
        fid = await _record(spark_db, CORA, "uploads/1/notes.txt", conversation_id=cid)
        row = await files_db.set_conversation_file_title(spark_db, ADA, cid, fid, "By Ada")
        assert row is not None and row["title"] == "By Ada"
        row = await files_db.set_conversation_file_title(spark_db, CORA, cid, fid, "By Cora")
        assert row is not None and row["title"] == "By Cora"

    async def test_a_viewer_and_a_stranger_cannot_rename(self, spark_db: Any) -> None:
        cid = await _shared(spark_db)
        fid = await _record(spark_db, CORA, "uploads/1/notes.txt", conversation_id=cid)
        with pytest.raises(NotPermitted):
            await files_db.set_conversation_file_title(spark_db, VIC, cid, fid, "Vic")
        with pytest.raises(NotOwned):
            await files_db.set_conversation_file_title(spark_db, SAM, cid, fid, "Sam")
        assert (await files_db.get_file_title(spark_db, CORA, fid)) == "notes.txt"

    async def test_a_file_of_another_conversation_is_not_renamed(self, spark_db: Any) -> None:
        cid = await _shared(spark_db)
        other = await conversations.create_conversation(spark_db, "Other", "stub/model", ADA)
        fid = await _record(spark_db, ADA, "a.txt", conversation_id=other)
        assert await files_db.set_conversation_file_title(spark_db, CORA, cid, fid, "x") is None
        assert (await files_db.get_file_title(spark_db, ADA, fid)) == "a.txt"


class TestReadable:
    async def test_members_reach_a_conversation_file_and_strangers_do_not(
        self, spark_db: Any
    ) -> None:
        cid = await _shared(spark_db)
        fid = await _record(spark_db, CORA, "charts/c.json", conversation_id=cid, kind="chart")
        for who in (ADA, CORA, VIC):
            row = await files_db.get_readable_file(spark_db, who, fid)
            assert row is not None and row["id"] == fid, who
        assert await files_db.get_readable_file(spark_db, SAM, fid) is None

    async def test_own_files_outside_a_conversation_only_for_the_owner(self, spark_db: Any) -> None:
        fid = await _record(spark_db, ADA, "charts/private.json", kind="chart")
        assert await files_db.get_readable_file(spark_db, ADA, fid) is not None
        assert await files_db.get_readable_file(spark_db, CORA, fid) is None

    async def test_a_removed_member_loses_it_at_once(self, spark_db: Any) -> None:
        cid = await _shared(spark_db)
        fid = await _record(spark_db, ADA, "charts/c.json", conversation_id=cid, kind="chart")
        await conversation_members.remove_member(spark_db, ADA, cid, VIC)
        assert await files_db.get_readable_file(spark_db, VIC, fid) is None

    async def test_conversation_artefacts_list_titles_and_kinds(self, spark_db: Any) -> None:
        cid = await _shared(spark_db)
        await _record(spark_db, ADA, "charts/c.json", conversation_id=cid, kind="chart", title="C")
        await _record(spark_db, CORA, "uploads/u.txt", conversation_id=cid)
        await _record(spark_db, ADA, "elsewhere.txt")
        rows = await files_db.list_conversation_artefacts(spark_db, VIC, cid)
        assert sorted((r["title"], r["kind"]) for r in rows) == [
            ("C", "chart"),
            ("u.txt", "attachment"),
        ]
        assert await files_db.list_conversation_artefacts(spark_db, SAM, cid) == []
