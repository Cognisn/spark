"""Tests for debate tool schemas and exhibit validation."""

from __future__ import annotations

from spark.core.debate.tools import (
    DELIVER_RULING_TOOL,
    MEMORY_TOOL_NAMES,
    REQUEST_NEXT_ROUND_TOOL,
    SET_SPEAKING_ORDER_TOOL,
    SUBMIT_ARGUMENT_TOOL,
    validate_exhibits,
)


class TestSchemas:
    def test_submit_argument_shape(self) -> None:
        assert SUBMIT_ARGUMENT_TOOL["name"] == "submit_argument"
        schema = SUBMIT_ARGUMENT_TOOL["inputSchema"]
        assert "argument_markdown" in schema["required"]
        ex_items = schema["properties"]["exhibits"]["items"]
        assert ex_items["properties"]["content"]["maxLength"] == 4000

    def test_procedural_tools(self) -> None:
        first = SET_SPEAKING_ORDER_TOOL["inputSchema"]["properties"]["first_speaker"]
        assert first["enum"] == ["pro", "con"]
        assert REQUEST_NEXT_ROUND_TOOL["name"] == "request_next_round"
        assert DELIVER_RULING_TOOL["name"] == "deliver_ruling"

    def test_memory_tools_listed(self) -> None:
        assert "store_memory" in MEMORY_TOOL_NAMES


class TestValidateExhibits:
    def test_normalises_labels_and_truncates(self) -> None:
        out = validate_exhibits(
            [
                {"label": "zz", "title": "T1", "content": "x" * 5000, "source": "s"},
                {"title": "T2", "content": "ok"},
                "not-a-dict",
            ]
        )
        assert [e["label"] for e in out] == ["A", "B"]
        assert len(out[0]["content"]) == 4000

    def test_none_gives_empty(self) -> None:
        assert validate_exhibits(None) == []
