"""A scripted model server for the live end-to-end stack (test only, standard library only).

It speaks enough of two provider APIs for SPARK's own provider code to run
unchanged against it:

- Ollama (``GET /api/tags``, ``POST /api/chat``), enabled in SPARK with
  ``providers.ollama.base_url``; every non-live check chats through it.
- Anthropic (``GET /v1/models``), reached when the SPARK containers set
  ``ANTHROPIC_BASE_URL`` (``compose.anthropic-stub.yaml``); the cross-replica
  key check reads which key each replica sent.

Replies are scripted by directives in the newest user message:

- ``[[tokens N]]`` replies with N tokens of text (SPARK counts an Ollama
  reply as ``len(text) // 4`` output tokens);
- ``[[sleep S]]`` waits S seconds before replying (a turn kept in flight);
- ``[[call NAME {json}]]`` calls the tool NAME with those arguments, once;
  the next request, carrying the tool's result, is answered with
  ``TOOL RESULT SEEN: <the result>`` so the suite can see what SPARK passed on.

A knowledge-graph extraction request (the ``record_graph_fragment`` tool) is
answered with one entity per ``KG(name)`` in the text and one relationship per
``KG(source relation target)``.

``GET /_stub/requests`` lists what was received (API keys as a short SHA-256
prefix, never the key); ``POST /_stub/reset`` clears it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OLLAMA_MODEL = "llama3-stub:latest"
ANTHROPIC_MODEL = "claude-stub-live-e2e"
WORD = "abcd"  # four characters: one token by SPARK's Ollama estimate

_DIRECTIVE = re.compile(r"\[\[(tokens|sleep|call)\s+([^\]]*?)\]\]")
_KG_ITEM = re.compile(r"KG\(([^)]+)\)")

_lock = threading.Lock()
_requests: list[dict[str, Any]] = []


def _record(entry: dict[str, Any], peer: str | None = None) -> None:
    with _lock:
        _requests.append({"at": time.time(), "peer": peer, **entry})


def _key_digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _directives(text: str) -> dict[str, str]:
    return {name: value.strip() for name, value in _DIRECTIVE.findall(text)}


def _graph_fragment(text: str) -> dict[str, Any]:
    entities: dict[str, dict[str, str]] = {}
    relationships = []
    for item in _KG_ITEM.findall(text):
        parts = item.split()
        if len(parts) == 3:
            source, relation, target = parts
            relationships.append({"source": source, "target": target, "relation": relation})
            names = [source, target]
        else:
            names = [item.strip()]
        for name in names:
            entities[name] = {"name": name, "entity_type": "concept", "description": name}
    return {"entities": list(entities.values()), "relationships": relationships}


def ollama_reply(body: dict[str, Any]) -> dict[str, Any]:
    """The assistant message for one ``/api/chat`` request."""
    messages = body.get("messages") or []
    tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    last = messages[-1] if messages else {}
    user_text = next(
        (_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), ""
    )
    if "record_graph_fragment" in tools:
        call = {
            "function": {"name": "record_graph_fragment", "arguments": _graph_fragment(user_text)}
        }
        return {"role": "assistant", "content": "", "tool_calls": [call]}

    found = _directives(user_text)
    if "sleep" in found:
        time.sleep(float(found["sleep"]))
    if last.get("role") == "tool":
        return {"role": "assistant", "content": "TOOL RESULT SEEN: " + _text(last.get("content"))}
    if "call" in found:
        name, _, raw = found["call"].partition(" ")
        call = {"function": {"name": name, "arguments": json.loads(raw or "{}")}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}
    tokens = int(found.get("tokens", "8"))
    return {"role": "assistant", "content": WORD * tokens}


class Handler(BaseHTTPRequestHandler):
    server_version = "spark-live-model-stub"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/_stub/requests":
            with _lock:
                self._json(200, list(_requests))
        elif path == "/api/tags":
            _record({"api": "ollama.tags"})
            model = {
                "name": OLLAMA_MODEL,
                "model": OLLAMA_MODEL,
                "modified_at": "2026-11-01T00:00:00Z",
                "size": 1,
                "digest": "0" * 64,
                "details": {"format": "gguf", "family": "llama", "parameter_size": "1B"},
            }
            self._json(200, {"models": [model]})
        elif path == "/api/version":
            self._json(200, {"version": "0.6.0"})
        elif path == "/v1/models":
            # The SDK sends the key as x-api-key or, from 2.x, as a bearer token.
            bearer = self.headers.get("Authorization", "")
            key = self.headers.get("x-api-key") or bearer.removeprefix("Bearer ").strip() or None
            _record({"api": "anthropic.models", "key": _key_digest(key)}, self.client_address[0])
            model = {
                "id": ANTHROPIC_MODEL,
                "type": "model",
                "display_name": "Claude stub (live e2e)",
                "created_at": "2026-11-01T00:00:00Z",
            }
            body = {"data": [model], "has_more": False, "first_id": ANTHROPIC_MODEL}
            self._json(200, {**body, "last_id": ANTHROPIC_MODEL})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/_stub/reset":
            with _lock:
                _requests.clear()
            self._json(200, {"status": "reset"})
            return
        if path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        body = self._body()
        messages = body.get("messages") or []
        _record(
            {
                "api": "ollama.chat",
                "model": body.get("model"),
                "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
                "last_role": messages[-1].get("role") if messages else None,
                "last": _text(messages[-1].get("content")) if messages else "",
                "user": next(
                    (
                        _text(m.get("content"))
                        for m in reversed(messages)
                        if m.get("role") == "user"
                    ),
                    "",
                ),
            },
            self.client_address[0],
        )
        message = ollama_reply(body)
        self._json(
            200,
            {
                "model": body.get("model") or OLLAMA_MODEL,
                "created_at": "2026-11-01T00:00:00Z",
                "message": message,
                "done": True,
                "done_reason": "stop",
            },
        )


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()  # noqa: S104 - container


if __name__ == "__main__":
    main()
