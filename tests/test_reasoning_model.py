"""Tests for ReasoningChatOpenAI custom model."""
from __future__ import annotations

import pytest

from src.harness.reasoning_model import ReasoningChatOpenAI


@pytest.fixture
def model() -> ReasoningChatOpenAI:
    return ReasoningChatOpenAI(
        model="qwen3.6-plus",
        api_key="test-key",
        base_url="https://example.com/v1",
        reasoning=True,
    )


def test_process_chunk_emits_reasoning_and_content(model: ReasoningChatOpenAI):
    state: list = []
    emitted: set = set()
    chunks = list(
        model._process_chunk(
            {"choices": [{"delta": {"reasoning_content": "I think", "content": "Hi"}}]},
            state,
            emitted,
        )
    )
    assert len(chunks) == 2
    assert chunks[0].message.additional_kwargs.get("reasoning_content") == "I think"
    assert chunks[1].message.content == "Hi"


def test_process_chunk_emits_tool_calls_only_once(model: ReasoningChatOpenAI):
    """Regression: tool_calls must not be re-emitted on subsequent empty/finish chunks."""
    state: list = []
    emitted: set = set()

    # Fragment 1: id + name
    chunks = list(
        model._process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "recall"}}
                            ]
                        }
                    }
                ]
            },
            state,
            emitted,
        )
    )
    assert len(chunks) == 0

    # Fragment 2: arguments start
    chunks = list(
        model._process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"query":'}}
                            ]
                        }
                    }
                ]
            },
            state,
            emitted,
        )
    )
    assert len(chunks) == 0

    # Fragment 3: arguments complete
    chunks = list(
        model._process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' "goal"}'}}
                            ]
                        }
                    }
                ]
            },
            state,
            emitted,
        )
    )
    assert len(chunks) == 1
    tool_calls = chunks[0].message.tool_calls
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["name"] == "recall"
    assert tool_calls[0]["args"] == {"query": "goal"}

    # Empty/finish chunk: must not re-emit the same tool call
    chunks = list(
        model._process_chunk(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            state,
            emitted,
        )
    )
    assert len(chunks) == 0

    # Another empty chunk: still nothing
    chunks = list(
        model._process_chunk(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            state,
            emitted,
        )
    )
    assert len(chunks) == 0


def test_process_chunk_emits_multiple_distinct_tools(model: ReasoningChatOpenAI):
    state: list = []
    emitted: set = set()

    # Send complete calls for two tools in one chunk
    chunks = list(
        model._process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                                },
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "function": {
                                        "name": "view_image",
                                        "arguments": '{"image_path": "x.png"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            },
            state,
            emitted,
        )
    )
    assert len(chunks) == 1
    tool_calls = chunks[0].message.tool_calls
    assert len(tool_calls) == 2
    assert tool_calls[0]["name"] == "list_dir"
    assert tool_calls[1]["name"] == "view_image"


def test_build_body_merges_system_messages(model: ReasoningChatOpenAI):
    """Regression: multiple system messages (e.g. rubric continuation) must be
    merged into one at the start, or Qwen/OpenAI API may reject the request."""
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    messages = [
        SystemMessage(content="You are a helpful agent."),
        HumanMessage(content="执行 mkdir foo"),
        AIMessage(
            content="",
            tool_calls=[{"name": "exec", "args": {"command": "mkdir foo"}, "id": "c1"}],
        ),
        ToolMessage(content="[exit 0]", tool_call_id="c1"),
        SystemMessage(content="任务似乎尚未真正完成，继续调用工具。"),
        AIMessage(content="好的，目录已创建。"),
    ]
    body = model._build_body(messages, stream=False)
    msgs = body["messages"]
    # Only one system message, at the front
    system_count = sum(1 for m in msgs if m["role"] == "system")
    assert system_count == 1
    assert msgs[0]["role"] == "system"
    # Both system contents are merged
    assert "helpful agent" in msgs[0]["content"]
    assert "尚未真正完成" in msgs[0]["content"]
    # Non-system messages follow in order
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"
    assert msgs[3]["role"] == "tool"
    assert msgs[4]["role"] == "assistant"
