"""Tests for knowledge graph extraction."""

from __future__ import annotations

from spark.knowledge.extractor import (
    RECORD_FRAGMENT_TOOL,
    chunk_messages,
    extract_fragment,
    render_chunk,
)


class ScriptedService:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def set_model(self, model_id: str) -> None:
        self._model = model_id

    def invoke_model(self, messages, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def tool_response(entities, relationships):
    return {
        "content": "",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": [
            {
                "id": "t1",
                "name": "record_graph_fragment",
                "input": {"entities": entities, "relationships": relationships},
            }
        ],
        "content_blocks": [],
    }


def text_response(text="no tool"):
    return {
        "content": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": None,
        "content_blocks": [],
    }


class TestChunking:
    def test_empty_and_order(self) -> None:
        assert chunk_messages([]) == []
        msgs = [{"id": i, "role": "user", "content": "x" * 5000} for i in range(5)]
        chunks = chunk_messages(msgs, chunk_chars=12000)
        assert [m["id"] for c in chunks for m in c] == [0, 1, 2, 3, 4]
        assert all(sum(len(m["content"]) for m in c) <= 12000 for c in chunks)

    def test_oversize_message_isolated(self) -> None:
        msgs = [{"id": 1, "role": "user", "content": "y" * 30000}]
        chunks = chunk_messages(msgs, chunk_chars=12000)
        assert len(chunks) == 1 and len(chunks[0][0]["content"]) == 12000


class TestExtractFragment:
    def test_happy_path_coerces_types(self) -> None:
        svc = ScriptedService(
            [
                tool_response(
                    [
                        {"name": "Spark", "entity_type": "banana", "description": "d"},
                        {"name": "", "entity_type": "person", "description": "dropped"},
                    ],
                    [
                        {
                            "source": "Spark",
                            "target": "Cognisn",
                            "relation": "belongs_to",
                            "description": "",
                        }
                    ],
                )
            ]
        )
        frag = extract_fragment(svc, "text")
        assert frag["entities"] == [{"name": "Spark", "entity_type": "other", "description": "d"}]
        assert frag["relationships"][0]["relation"] == "belongs_to"

    def test_retry_then_success(self) -> None:
        svc = ScriptedService([text_response(), tool_response([{"name": "A"}], [])])
        frag = extract_fragment(svc, "text")
        assert svc.calls == 2 and frag["entities"][0]["name"] == "A"

    def test_double_failure_returns_none(self) -> None:
        svc = ScriptedService([text_response(), text_response()])
        assert extract_fragment(svc, "text") is None


def test_render_chunk() -> None:
    out = render_chunk(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )
    assert "USER: hi" in out and "ASSISTANT: hello" in out


def test_tool_shape() -> None:
    schema = RECORD_FRAGMENT_TOOL["inputSchema"]
    assert "entities" in schema["properties"] and "relationships" in schema["properties"]
