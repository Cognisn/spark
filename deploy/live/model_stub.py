"""A scripted model server for the live end-to-end stack (test only, standard library only).

It speaks enough of two provider APIs for conduit's own adapters to run
unchanged against it, inside the stack:

- Ollama (``GET /api/tags``, ``POST /api/chat``, streamed as NDJSON) on plain
  http, port 11434: SPARK's Ollama connections (``http://model-stub:11434``)
  chat through it. It lists ``llama3-stub``, ``tiny-stub`` and ``extra-stub``,
  none of which conduit's catalogue declares, so each is declared as a
  SPARK catalogue overlay before it is offered.
- Anthropic (``GET /v1/models``) on https, port 11443, with a certificate from
  the stack's throwaway CA (``model-stub.crt``): an Anthropic connection whose
  ``base_url`` is ``https://model-stub:11443`` discovers through it. It lists one
  model conduit's catalogue declares and one it does not, and records a digest
  of the key each caller sent.

Replies are scripted by directives in the newest user message:

- ``[[tokens N]]`` replies with N tokens of text (``abcd`` N times); the reply
  reports ``eval_count`` N, so conduit records N output tokens;
- ``[[prompt N]]`` reports ``prompt_eval_count`` N (default 10, whatever the
  prompt's real size), so the credits a turn costs are predictable;
- ``[[sleep S]]`` waits S seconds before replying (a turn kept in flight);
- ``[[call NAME {json}]]`` calls the tool NAME with those arguments, once;
  the next request, carrying the tool's result, is answered with
  ``TOOL RESULT SEEN: <the result>`` so the suite can see what SPARK passed on;
- ``[[call64 NAME base64]]`` does the same with the arguments as base64 of their
  JSON, for arguments holding a ``]`` (a list), which ``call`` cannot carry.

A knowledge-graph extraction request (the ``record_graph_fragment`` tool) is
answered with one entity per ``KG(name)`` in the text and one relationship per
``KG(source relation target)``.

``GET /_stub/requests`` lists what was received (API keys as a short SHA-256
prefix, never the key; the system prompt as sent, which holds only synthetic
text); ``POST /_stub/reset`` clears it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OLLAMA_MODELS = ("llama3-stub:latest", "tiny-stub:latest", "extra-stub:latest")
#: One model conduit's catalogue declares for Anthropic, and one it does not.
ANTHROPIC_MODELS = ("claude-haiku-5-5", "claude-stub-live-e2e")
WORD = "abcd"  # four characters
PROMPT_TOKENS = 10
CERT_DIR = os.environ.get("LIVE_STUB_CERTS", "/etc/spark-live")
SYSTEM_KEPT = 50_000  # characters of a system prompt kept in the record

_DIRECTIVE = re.compile(r"\[\[(tokens|sleep|call64|call|prompt)\s+([^\]]*?)\]\]")
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


def _user_text(messages: list[dict[str, Any]]) -> str:
    return next(
        (_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), ""
    )


def ollama_reply(body: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    """The assistant message for one ``/api/chat`` request, with its prompt and output counts."""
    messages = body.get("messages") or []
    tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    last = messages[-1] if messages else {}
    user_text = _user_text(messages)
    found = _directives(user_text)
    prompt = int(found.get("prompt", PROMPT_TOKENS))
    if "record_graph_fragment" in tools:
        call = {
            "function": {"name": "record_graph_fragment", "arguments": _graph_fragment(user_text)}
        }
        return {"role": "assistant", "content": "", "tool_calls": [call]}, prompt, 1

    if "sleep" in found:
        time.sleep(float(found["sleep"]))
    if last.get("role") == "tool":
        text = "TOOL RESULT SEEN: " + _text(last.get("content"))
        return {"role": "assistant", "content": text}, prompt, max(1, len(text) // 4)
    if "call64" in found:
        name, _, raw = found["call64"].partition(" ")
        arguments = json.loads(base64.b64decode(raw.strip()) or b"{}")
        call = {"function": {"name": name, "arguments": arguments}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, prompt, 1
    if "call" in found:
        name, _, raw = found["call"].partition(" ")
        call = {"function": {"name": name, "arguments": json.loads(raw or "{}")}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, prompt, 1
    tokens = int(found.get("tokens", "8"))
    return {"role": "assistant", "content": WORD * tokens}, prompt, tokens


class Handler(BaseHTTPRequestHandler):
    server_version = "spark-live-model-stub"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

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
            _record({"api": "ollama.tags"}, self.client_address[0])
            models = [
                {
                    "name": name,
                    "model": name,
                    "modified_at": "2026-11-01T00:00:00Z",
                    "size": 1,
                    "digest": "0" * 64,
                    "details": {"format": "gguf", "family": "llama", "parameter_size": "1B"},
                }
                for name in OLLAMA_MODELS
            ]
            self._json(200, {"models": models})
        elif path == "/api/version":
            self._json(200, {"version": "0.6.0"})
        elif path == "/v1/models":
            # The SDK sends the key as x-api-key or, from 2.x, as a bearer token.
            bearer = self.headers.get("Authorization", "")
            key = self.headers.get("x-api-key") or bearer.removeprefix("Bearer ").strip() or None
            _record({"api": "anthropic.models", "key": _key_digest(key)}, self.client_address[0])
            data = [
                {
                    "id": model_id,
                    "type": "model",
                    "display_name": model_id,
                    "created_at": "2026-11-01T00:00:00Z",
                }
                for model_id in ANTHROPIC_MODELS
            ]
            self._json(
                200,
                {
                    "data": data,
                    "has_more": False,
                    "first_id": ANTHROPIC_MODELS[0],
                    "last_id": ANTHROPIC_MODELS[-1],
                },
            )
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
        system = next((_text(m.get("content")) for m in messages if m.get("role") == "system"), "")
        _record(
            {
                "api": "ollama.chat",
                "model": body.get("model"),
                "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
                "last_role": messages[-1].get("role") if messages else None,
                "last": _text(messages[-1].get("content")) if messages else "",
                "user": _user_text(messages),
                "system": system[:SYSTEM_KEPT],
                "num_ctx": (body.get("options") or {}).get("num_ctx"),
            },
            self.client_address[0],
        )
        message, prompt, output = ollama_reply(body)
        line = {
            "model": body.get("model") or OLLAMA_MODELS[0],
            "created_at": "2026-11-01T00:00:00Z",
            "message": message,
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": prompt,
            "eval_count": output,
        }
        self._send(200, (json.dumps(line) + "\n").encode(), "application/x-ndjson")


def _tls_server() -> ThreadingHTTPServer | None:
    """The https listener for the Anthropic API, when the stack gave it a certificate."""
    cert = os.path.join(CERT_DIR, "model-stub.crt")
    key = os.path.join(CERT_DIR, "model-stub.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        return None
    server = ThreadingHTTPServer(("0.0.0.0", 11443), Handler)  # noqa: S104 - container
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    tls = _tls_server()
    if tls is not None:
        threading.Thread(target=tls.serve_forever, name="anthropic-tls", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()  # noqa: S104 - container


if __name__ == "__main__":
    main()
