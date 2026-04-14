"""Server-Sent Events streaming for real-time chat responses."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream")


@router.get("/chat")
async def stream_chat(request: Request) -> EventSourceResponse:
    """SSE endpoint for streaming chat responses."""
    message = request.query_params.get("message", "")
    conversation_id = int(request.query_params.get("conversation_id", "0"))

    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:

        async def error_gen() -> AsyncGenerator[dict, None]:
            yield {"event": "error", "data": json.dumps({"message": "Not initialised"})}

        return EventSourceResponse(error_gen())

    user_guid = getattr(request.app.state, "user_guid", "default")

    # Shared state for permission requests
    pending_permissions: dict[str, threading.Event] = {}
    permission_responses: dict[str, str] = {}

    # Store on app state so the /permission/respond endpoint can access them
    if not hasattr(request.app.state, "permission_events"):
        request.app.state.permission_events = {}
    if not hasattr(request.app.state, "permission_responses"):
        request.app.state.permission_responses = {}

    async def event_generator() -> AsyncGenerator[dict, None]:
        yield {"event": "status", "data": json.dumps({"status": "processing"})}

        tool_events: list[dict] = []
        final_content = ""

        def status_callback(event_type: str, data: dict) -> None:
            """Collect tool events from the conversation manager."""
            tool_events.append({"event": event_type, "data": data})

        def stream_callback(text: str) -> None:
            nonlocal final_content
            final_content += text

        def permission_callback(tool_name: str, tool_input: dict) -> str:
            """Called from the tool execution thread when permission is needed.

            Emits a permission request event and blocks until the user responds
            via the /permission/respond endpoint.
            """
            request_id = str(uuid.uuid4())[:8]

            # Create an event that the main loop will emit as SSE
            tool_events.append(
                {
                    "event": "permission_request",
                    "data": {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "params": tool_input,
                    },
                }
            )

            # Create a threading event to wait on
            wait_event = threading.Event()
            request.app.state.permission_events[request_id] = wait_event
            request.app.state.permission_responses[request_id] = None

            logger.info(
                "Permission requested for tool %s (id=%s), waiting for user...",
                tool_name,
                request_id,
            )

            # Block until user responds (timeout after 120s)
            if wait_event.wait(timeout=120):
                decision = request.app.state.permission_responses.get(request_id, "denied")
            else:
                decision = "denied"
                logger.warning("Permission request %s timed out for tool %s", request_id, tool_name)

            # Cleanup
            request.app.state.permission_events.pop(request_id, None)
            request.app.state.permission_responses.pop(request_id, None)

            logger.info("Permission for %s: %s", tool_name, decision)
            return decision

        # Inject the permission callback into the conversation manager for this request
        original_callback = conv_mgr._tool_permission_callback
        conv_mgr._tool_permission_callback = permission_callback

        try:
            loop = asyncio.get_event_loop()
            result_future = loop.run_in_executor(
                None,
                lambda: conv_mgr.send_message(
                    conversation_id,
                    message,
                    user_guid,
                    status_callback=status_callback,
                    stream_callback=stream_callback,
                ),
            )

            # Poll for events while waiting for completion
            while not result_future.done():
                while tool_events:
                    evt = tool_events.pop(0)
                    event_type = evt["event"]
                    event_data = evt["data"]

                    if event_type == "tool_call":
                        yield {
                            "event": "tool_start",
                            "data": json.dumps(
                                {
                                    "tool_use_id": event_data.get("tool_use_id", ""),
                                    "tool_name": event_data.get("tool_name", ""),
                                    "params": event_data.get("params", {}),
                                }
                            ),
                        }
                    elif event_type == "tool_result":
                        yield {
                            "event": "tool_complete",
                            "data": json.dumps(
                                {
                                    "tool_use_id": event_data.get("tool_use_id", ""),
                                    "tool_name": event_data.get("tool_name", ""),
                                    "result": event_data.get("result", ""),
                                    "status": event_data.get("status", "success"),
                                }
                            ),
                        }
                    elif event_type == "permission_request":
                        yield {
                            "event": "permission_request",
                            "data": json.dumps(event_data),
                        }
                    elif event_type == "compaction_start":
                        yield {
                            "event": "compaction_status",
                            "data": json.dumps({"status": "start", **event_data}),
                        }
                    elif event_type == "compaction_complete":
                        yield {
                            "event": "compaction_status",
                            "data": json.dumps({"status": "complete", **event_data}),
                        }
                    elif event_type == "tool_iteration_complete":
                        yield {"event": "progress", "data": json.dumps(event_data)}
                    elif event_type == "agent_start":
                        yield {
                            "event": "agent_start",
                            "data": json.dumps({
                                "agent_id": event_data.get("agent_id", ""),
                                "agent_name": event_data.get("agent_name", ""),
                                "task": event_data.get("task", ""),
                                "model_id": event_data.get("model_id", ""),
                                "mode": event_data.get("mode", ""),
                            }),
                        }
                    elif event_type == "agent_tool_call":
                        yield {
                            "event": "agent_tool_call",
                            "data": json.dumps({
                                "agent_id": event_data.get("agent_id", ""),
                                "tool_name": event_data.get("tool_name", ""),
                                "params": event_data.get("params", {}),
                            }),
                        }
                    elif event_type == "agent_tool_result":
                        yield {
                            "event": "agent_tool_result",
                            "data": json.dumps({
                                "agent_id": event_data.get("agent_id", ""),
                                "tool_name": event_data.get("tool_name", ""),
                                "result": event_data.get("result", ""),
                                "status": event_data.get("status", "success"),
                            }),
                        }
                    elif event_type == "agent_complete":
                        yield {
                            "event": "agent_complete",
                            "data": json.dumps({
                                "agent_id": event_data.get("agent_id", ""),
                                "agent_name": event_data.get("agent_name", ""),
                                "status": event_data.get("status", "completed"),
                                "result": event_data.get("result", ""),
                                "input_tokens": event_data.get("input_tokens", 0),
                                "output_tokens": event_data.get("output_tokens", 0),
                            }),
                        }

                if final_content:
                    yield {
                        "event": "response",
                        "data": json.dumps({"content": final_content, "final": False}),
                    }
                    final_content = ""

                await asyncio.sleep(0.2)

            # Get the final result
            try:
                result = result_future.result()

                for evt in tool_events:
                    event_type = evt["event"]
                    event_data = evt["data"]
                    if event_type == "tool_call":
                        yield {"event": "tool_start", "data": json.dumps(event_data)}
                    elif event_type == "tool_result":
                        yield {"event": "tool_complete", "data": json.dumps(event_data)}

                yield {
                    "event": "response",
                    "data": json.dumps(
                        {
                            "content": result.get("content", ""),
                            "final": True,
                            "usage": result.get("usage", {}),
                            "tool_calls": len(result.get("tool_calls", [])),
                            "iterations": result.get("iterations", 1),
                        }
                    ),
                }
                yield {"event": "complete", "data": json.dumps({"status": "ok"})}

            except Exception as e:
                logger.error("Streaming error: %s", e)
                yield {"event": "error", "data": json.dumps({"message": str(e)})}

        finally:
            # Restore original callback
            conv_mgr._tool_permission_callback = original_callback

    return EventSourceResponse(event_generator())
