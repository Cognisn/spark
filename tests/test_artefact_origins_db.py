"""Which message produced or attached a file (the artefacts panel scrolls to it), on SQLite
and PostgreSQL.

A message names a file by a card line's ``"file_id": 12`` (escaped inside a tool result),
by a create tool's "kept in your files as file 12" or by an attachment note's
"- file 12: name". The first message that names a file is its origin; a longer id that
starts the same way is another file; a message of a conversation the reader may not read
is never looked at; artefact titles are looked up by id.
"""

from __future__ import annotations

import json
from typing import Any

from spark.database import conversation_members, conversations
from spark.database import files as files_db
from spark.database import messages
from spark.database.scope import Role

ADA = "user-ada-0001"
CORA = "user-cora-0002"
SAM = "user-sam-0004"


async def _chat(db: Any) -> int:
    cid = await conversations.create_conversation(db, "Plan", "stub/model", ADA)
    await conversation_members.share(db, ADA, cid, CORA, Role.VIEWER)
    return cid


async def _say(db: Any, cid: int, role: str, text: str) -> int:
    return await messages.add_message(db, cid, role, text, 1, ADA)


class TestOrigins:
    async def test_each_way_of_naming_a_file(self, spark_db: Any) -> None:
        cid = await _chat(spark_db)
        card = await _say(
            spark_db,
            cid,
            "user",
            json.dumps(
                [
                    {
                        "type": "tool_result",
                        "content": '[spark-preview] {"file_id":12,"kind":"chart"}',
                    }
                ]
            ),
        )
        plain = await _say(spark_db, cid, "user", 'x [spark-preview] {"file_id": 7}')
        made = await _say(spark_db, cid, "user", "Made it, kept in your files as file 31.")
        attached = await _say(spark_db, cid, "user", "Read this\n\n- file 44: notes.txt")
        found = await files_db.artefact_origins(spark_db, ADA, cid, [12, 7, 31, 44, 99])
        assert found == {12: card, 7: plain, 31: made, 44: attached}

    async def test_the_first_message_wins_and_ids_match_whole(self, spark_db: Any) -> None:
        cid = await _chat(spark_db)
        first = await _say(spark_db, cid, "user", "kept in your files as file 12.")
        await _say(spark_db, cid, "assistant", "I read file 12 again")
        longer = await _say(spark_db, cid, "user", "kept in your files as file 123.")
        found = await files_db.artefact_origins(spark_db, ADA, cid, [12, 123, 1])
        assert found == {12: first, 123: longer}

    async def test_only_a_reader_gets_origins(self, spark_db: Any) -> None:
        cid = await _chat(spark_db)
        await _say(spark_db, cid, "user", "kept in your files as file 5.")
        assert await files_db.artefact_origins(spark_db, CORA, cid, [5]) != {}
        assert await files_db.artefact_origins(spark_db, SAM, cid, [5]) == {}
        assert await files_db.artefact_origins(spark_db, ADA, cid, []) == {}

    async def test_titles_by_id(self, spark_db: Any) -> None:
        first = await files_db.record_user_file(
            spark_db,
            ADA,
            files_db.NewUserFile(key="a/one.txt", filename="one.txt", size=1, title="First"),
            quota_bytes=None,
        )
        second = await files_db.record_user_file(
            spark_db,
            ADA,
            files_db.NewUserFile(key="a/two.pdf", filename="two.pdf", size=1, source="preview"),
            quota_bytes=None,
        )
        assert await files_db.artefact_titles(spark_db, [first, second, 999]) == {
            first: ("First", "attachment"),
            second: ("two.pdf", "preview"),
        }
        assert await files_db.artefact_titles(spark_db, []) == {}
