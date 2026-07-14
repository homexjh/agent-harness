"""Tests for the QwenPaw-style SSE envelope reasoning phase logic."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from src.server.app import _run_envelope
from src.server.sse_utils import sse


def _parse_sse_events(payload: str) -> list[dict]:
    """Parse server-sent events from a raw string."""
    events = []
    for block in payload.strip().split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].lstrip(" ")
        if data:
            try:
                events.append({"event": event, "data": json.loads(data)})
            except json.JSONDecodeError:
                events.append({"event": event, "data": data})
    return events


@pytest.mark.anyio
async def test_reasoning_split_into_phases_by_tool_results():
    """Reasoning from two LLM calls should be emitted as separate phases (0 and 1)."""

    async def fake_astream(*args, **kwargs) -> AsyncIterator[tuple[Any, dict]]:
        # First LLM call: reasoning then a tool call
        yield (
            AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "I need to check the weather."},
            ),
            {"ls_provider": "reasoning-model"},
        )
        yield (
            AIMessageChunk(
                content="",
                additional_kwargs={},
                tool_call_chunks=[
                    {"id": "call_1", "name": "exec", "args": '{"command": "curl wttr.in"}', "index": 0}
                ],
            ),
            {"ls_provider": "reasoning-model"},
        )
        # Tool execution result
        yield ToolMessage(content="Sunny 25C", tool_call_id="call_1"), {}
        # Second LLM call: reasoning then final answer
        yield (
            AIMessageChunk(
                content="The weather is sunny.",
                additional_kwargs={"reasoning_content": "Now I can answer."},
            ),
            {"ls_provider": "reasoning-model"},
        )

    fake_graph = MagicMock()
    fake_graph.astream = fake_astream
    fake_graph.aget_state = AsyncMock(return_value=MagicMock(tasks=[]))

    fake_metrics = MagicMock()
    fake_metrics.snapshot.return_value = {}
    fake_metrics.gate_triggers = {}
    fake_metrics.errors = 0

    with patch("src.server.app.get_request_graph", return_value=(fake_graph, True)):
        with patch("src.server.app.get_metrics", return_value=fake_metrics):
            with patch("src.server.app._touch_session"):
                chunks = []
                async for chunk in _run_envelope("thread-1", {
                    "message": "weather?",
                    "model": "test",
                    "reasoning": True,
                }):
                    chunks.append(chunk)

    payload = "".join(chunks)
    events = _parse_sse_events(payload)

    reasoning_events = [e for e in events if e["event"] == "reasoning"]
    assert len(reasoning_events) == 2, f"expected 2 reasoning events, got {reasoning_events}"
    assert reasoning_events[0]["data"]["phase"] == 0
    assert reasoning_events[1]["data"]["phase"] == 1
    assert reasoning_events[0]["data"]["delta"] == "I need to check the weather."
    assert reasoning_events[1]["data"]["delta"] == "Now I can answer."
