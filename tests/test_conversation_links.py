"""Tests for conversation links database module."""

import pytest

from spark.database import conversation_links, conversations
from spark.db import Database

USER = "test-user"


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


@pytest.fixture()
def conn(spark_db):
    return spark_db


class TestConversationLinks:
    async def test_add_link(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "A", "m1", USER)
        c2 = await conversations.create_conversation(conn, "B", "m1", USER)
        assert await conversation_links.add_link(conn, c1, c2, USER) is True

    async def test_add_self_link_fails(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "A", "m1", USER)
        assert await conversation_links.add_link(conn, c1, c1, USER) is False

    async def test_add_duplicate_link_fails(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "A", "m1", USER)
        c2 = await conversations.create_conversation(conn, "B", "m1", USER)
        await conversation_links.add_link(conn, c1, c2, USER)
        assert await conversation_links.add_link(conn, c1, c2, USER) is False

    async def test_get_links(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "Source", "m1", USER)
        c2 = await conversations.create_conversation(conn, "Target 1", "m1", USER)
        c3 = await conversations.create_conversation(conn, "Target 2", "m1", USER)
        await conversation_links.add_link(conn, c1, c2, USER)
        await conversation_links.add_link(conn, c1, c3, USER)
        links = await conversation_links.get_links(conn, c1, USER)
        assert len(links) == 2
        names = {l["name"] for l in links}
        assert "Target 1" in names
        assert "Target 2" in names

    async def test_get_links_one_directional(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "A", "m1", USER)
        c2 = await conversations.create_conversation(conn, "B", "m1", USER)
        await conversation_links.add_link(conn, c1, c2, USER)
        # c2 should not see c1 as linked
        assert len(await conversation_links.get_links(conn, c2, USER)) == 0

    async def test_remove_link(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "A", "m1", USER)
        c2 = await conversations.create_conversation(conn, "B", "m1", USER)
        await conversation_links.add_link(conn, c1, c2, USER)
        await conversation_links.remove_link(conn, c1, c2, USER)
        assert len(await conversation_links.get_links(conn, c1, USER)) == 0

    async def test_get_linkable_excludes_self_and_linked(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "Source", "m1", USER)
        c2 = await conversations.create_conversation(conn, "Already Linked", "m1", USER)
        c3 = await conversations.create_conversation(conn, "Available", "m1", USER)
        await conversation_links.add_link(conn, c1, c2, USER)
        available = await conversation_links.get_linkable_conversations(conn, c1, USER)
        ids = {c["id"] for c in available}
        assert c3 in ids
        assert c1 not in ids
        assert c2 not in ids

    async def test_get_linkable_empty(self, conn: Database) -> None:
        c1 = await conversations.create_conversation(conn, "Only", "m1", USER)
        available = await conversation_links.get_linkable_conversations(conn, c1, USER)
        assert len(available) == 0
