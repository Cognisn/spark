"""Tests for conversation links database module."""

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.database import conversations, conversation_links

USER = "test-user"


@pytest.fixture()
def db(tmp_path) -> Database:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    return Database(conn)


@pytest.fixture()
def conn(db: Database) -> DatabaseConnection:
    return db.connection


class TestConversationLinks:
    def test_add_link(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "A", "m1", USER)
        c2 = conversations.create_conversation(conn, "B", "m1", USER)
        assert conversation_links.add_link(conn, c1, c2, USER) is True

    def test_add_self_link_fails(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "A", "m1", USER)
        assert conversation_links.add_link(conn, c1, c1, USER) is False

    def test_add_duplicate_link_fails(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "A", "m1", USER)
        c2 = conversations.create_conversation(conn, "B", "m1", USER)
        conversation_links.add_link(conn, c1, c2, USER)
        assert conversation_links.add_link(conn, c1, c2, USER) is False

    def test_get_links(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "Source", "m1", USER)
        c2 = conversations.create_conversation(conn, "Target 1", "m1", USER)
        c3 = conversations.create_conversation(conn, "Target 2", "m1", USER)
        conversation_links.add_link(conn, c1, c2, USER)
        conversation_links.add_link(conn, c1, c3, USER)
        links = conversation_links.get_links(conn, c1, USER)
        assert len(links) == 2
        names = {l["name"] for l in links}
        assert "Target 1" in names
        assert "Target 2" in names

    def test_get_links_one_directional(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "A", "m1", USER)
        c2 = conversations.create_conversation(conn, "B", "m1", USER)
        conversation_links.add_link(conn, c1, c2, USER)
        # c2 should not see c1 as linked
        assert len(conversation_links.get_links(conn, c2, USER)) == 0

    def test_remove_link(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "A", "m1", USER)
        c2 = conversations.create_conversation(conn, "B", "m1", USER)
        conversation_links.add_link(conn, c1, c2, USER)
        conversation_links.remove_link(conn, c1, c2, USER)
        assert len(conversation_links.get_links(conn, c1, USER)) == 0

    def test_get_linkable_excludes_self_and_linked(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "Source", "m1", USER)
        c2 = conversations.create_conversation(conn, "Already Linked", "m1", USER)
        c3 = conversations.create_conversation(conn, "Available", "m1", USER)
        conversation_links.add_link(conn, c1, c2, USER)
        available = conversation_links.get_linkable_conversations(conn, c1, USER)
        ids = {c["id"] for c in available}
        assert c3 in ids
        assert c1 not in ids
        assert c2 not in ids

    def test_get_linkable_empty(self, conn: DatabaseConnection) -> None:
        c1 = conversations.create_conversation(conn, "Only", "m1", USER)
        available = conversation_links.get_linkable_conversations(conn, c1, USER)
        assert len(available) == 0
