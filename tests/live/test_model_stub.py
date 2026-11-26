"""The live stack's scripted model server, checked without a stack (the default suite runs it).

``deploy/live/model_stub.py`` is standard library only and is loaded here from its
file, as the stack's ``model-stub`` container runs it. These checks cover the reply
directives the live suite scripts its turns with.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

STUB = Path(__file__).resolve().parents[2] / "deploy" / "live" / "model_stub.py"


@pytest.fixture(scope="module")
def stub() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_model_stub", STUB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("live_model_stub", module)
    spec.loader.exec_module(module)
    return module


def _reply(stub: ModuleType, text: str) -> dict[str, Any]:
    message, _prompt, _output = stub.ollama_reply(
        {"messages": [{"role": "user", "content": text}], "tools": []}
    )
    found: dict[str, Any] = message
    return found


def _b64(arguments: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(arguments).encode()).decode()


def test_call_takes_json_without_brackets(stub: ModuleType) -> None:
    message = _reply(stub, 'please [[call read_my_file {"file_id": 7}]]')
    assert message["tool_calls"] == [
        {"function": {"name": "read_my_file", "arguments": {"file_id": 7}}}
    ]


def test_call64_carries_arguments_with_arrays(stub: ModuleType) -> None:
    arguments = {
        "title": "Units sold by quarter",
        "definition": {
            "type": "bar",
            "data": {"columns": ["quarter", "units"], "rows": [["Q1", 3], ["Q2", 5]]},
            "encoding": {"x": "quarter", "y": "units"},
        },
    }
    message = _reply(stub, f"draw it [[call64 create_chart {_b64(arguments)}]]")
    assert message["tool_calls"] == [{"function": {"name": "create_chart", "arguments": arguments}}]


def test_call64_answers_the_tool_result_like_call(stub: ModuleType) -> None:
    arguments = {"names": ["a", "b"]}
    message, _prompt, _output = stub.ollama_reply(
        {
            "messages": [
                {"role": "user", "content": f"[[call64 some_tool {_b64(arguments)}]]"},
                {"role": "assistant", "content": ""},
                {"role": "tool", "content": "the result"},
            ]
        }
    )
    assert message == {"role": "assistant", "content": "TOOL RESULT SEEN: the result"}
