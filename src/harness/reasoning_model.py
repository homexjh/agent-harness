"""Custom OpenAI-compatible chat model that captures reasoning_content.

Qwen/DeepSeek/OpenAI reasoning models return ``reasoning_content`` in the stream delta,
but ``langchain-openai 1.3.x`` + ``openai 2.44.0`` silently drop that field. This model
uses ``httpx`` directly to stream the raw OpenAI-compatible response and converts it
back into LangChain ``AIMessageChunk`` objects with ``additional_kwargs.reasoning_content``,
so the rest of the harness (LangGraph tool binding + app.py SSE envelope) can consume
thinking traces without any other changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

logger = logging.getLogger("agent_harness.reasoning")


def _content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "".join(parts)
    return str(content)


def _lc_message_to_openai(m: BaseMessage) -> Dict[str, Any]:
    if m.type == "human":
        return {"role": "user", "content": _content_to_str(m.content)}
    if m.type == "system":
        return {"role": "system", "content": _content_to_str(m.content)}
    if m.type == "ai":
        d: Dict[str, Any] = {"role": "assistant", "content": _content_to_str(m.content) or ""}
        tool_calls = getattr(m, "tool_calls", None) or []
        if tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"]) if isinstance(tc["args"], dict) else str(tc["args"]),
                    },
                }
                for tc in tool_calls
            ]
        return d
    if m.type == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": _content_to_str(m.content)}
    # Fallback: treat as user
    return {"role": "user", "content": _content_to_str(m.content)}


class ReasoningChatOpenAI(BaseChatModel):
    """OpenAI-compatible chat model with explicit reasoning_content capture.

    Parameters match ``ChatOpenAI`` where possible so it can be used as a drop-in
    replacement when ``reasoning=True`` is requested.
    """

    model: str = Field(description="Model name to use")
    api_key: str = Field(description="API key")
    base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI-compatible endpoint")
    temperature: float = Field(default=0.3)
    reasoning: bool = Field(default=False)
    max_tokens: Optional[int] = Field(default=None)
    _tools: List[Dict[str, Any]] = []
    _bound: bool = False

    def bind_tools(self, tools: List[BaseTool], **kwargs: Any) -> "ReasoningChatOpenAI":
        """Bind tools just like ChatOpenAI."""
        self._tools = [convert_to_openai_tool(t) for t in tools]
        self._bound = True
        # 透传 max_tokens（chat 轨用它限制闲聊铺陈，对齐 QwenPaw 的 max_tokens 注入）
        if "max_tokens" in kwargs:
            self.max_tokens = kwargs["max_tokens"]
        return self

    @property
    def _llm_type(self) -> str:
        return "reasoning-chat-openai"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, messages: List[BaseMessage], stream: bool) -> Dict[str, Any]:
        # 合并所有 system 消息为一条放在最前面。
        # 原因：Qwen / OpenAI 兼容接口可能拒绝历史中间出现的 system 消息
        #（如 rubric gate 注入的续跑提示、context manager 的折叠桩），
        # 合并后避免 400 错误。
        system_parts: List[str] = []
        non_system: List[BaseMessage] = []
        for m in messages:
            if m.type == "system":
                txt = _content_to_str(m.content)
                if txt:
                    system_parts.append(txt)
            else:
                non_system.append(m)
        openai_msgs: List[Dict[str, Any]] = []
        if system_parts:
            openai_msgs.append({"role": "system", "content": "\n\n".join(system_parts)})
        openai_msgs.extend(_lc_message_to_openai(m) for m in non_system)

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": openai_msgs,
            "stream": stream,
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.reasoning:
            # 推理模式：开启 thinking（dashscope 兼容模式用 enable_thinking 控制）
            body["reasoning"] = {"type": "enabled"}
            body["enable_thinking"] = True
        else:
            # 非推理模式：对默认 thinking=ON 的厂商显式关闭，避免后台隐藏推理拖慢耗时
            # （qwen3.x 在 dashscope 兼容模式默认 thinking=ON，不关会让闲聊慢数倍）
            body["enable_thinking"] = False
        if self._tools:
            body["tools"] = self._tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _parse_sse(lines: List[str]) -> Optional[Dict[str, Any]]:
        parts = []
        for line in lines:
            if line.startswith("data: "):
                parts.append(line[6:])
        if not parts:
            return None
        data = "".join(parts)
        if data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def _process_chunk(
        self, data: Dict[str, Any], tool_calls_state: List[Dict[str, Any]], emitted_indices: set
    ) -> Iterator[ChatGenerationChunk]:
        delta = (data.get("choices") or [{}])[0].get("delta") or {}
        content = delta.get("content") or ""
        reasoning_content = delta.get("reasoning_content") or ""
        delta_tool_calls = delta.get("tool_calls")
        if delta_tool_calls:
            for d in delta_tool_calls:
                idx = d.get("index", 0)
                while len(tool_calls_state) <= idx:
                    tool_calls_state.append({"id": "", "name": "", "arguments": ""})
                tc = tool_calls_state[idx]
                if d.get("id"):
                    tc["id"] = d["id"]
                func = d.get("function") or {}
                if func.get("name"):
                    tc["name"] = func["name"]
                if func.get("arguments"):
                    tc["arguments"] += func["arguments"]

        if reasoning_content:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": reasoning_content},
                )
            )

        if content:
            yield ChatGenerationChunk(message=AIMessageChunk(content=content))

        # Emit tool_calls only when we have received the complete call and have not
        # emitted it yet. A complete call has id + name + arguments that are valid JSON.
        # We track emitted indices to avoid re-emitting the same call on subsequent
        # empty/finish chunks, which would otherwise cause duplicate tool executions
        # and malformed argument strings downstream.
        if not content and not reasoning_content and tool_calls_state:
            complete_calls = []
            for i, tc in enumerate(tool_calls_state):
                if i in emitted_indices:
                    continue
                if not (tc["id"] and tc["name"] and tc["arguments"]):
                    continue
                try:
                    args = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    # Arguments are still incomplete; wait for the next fragment.
                    continue
                emitted_indices.add(i)
                complete_calls.append({"id": tc["id"], "name": tc["name"], "args": args})
            if complete_calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="", tool_calls=complete_calls)
                )

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        body = self._build_body(messages, stream=True)
        headers = self._get_headers()
        url = self.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
            with client.stream("POST", url, headers=headers, json=body) as response:
                response.raise_for_status()
                event_lines: List[str] = []
                tool_calls_state: List[Dict[str, Any]] = []
                emitted_indices: set = set()
                for line in response.iter_lines():
                    if line:
                        event_lines.append(line)
                    else:
                        data = self._parse_sse(event_lines)
                        event_lines = []
                        if data:
                            yield from self._process_chunk(data, tool_calls_state, emitted_indices)

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        body = self._build_body(messages, stream=True)
        headers = self._get_headers()
        url = self.base_url.rstrip("/") + "/chat/completions"
        msg_count = len(body.get("messages", []))
        logger.info("LLM_CALL_START model=%s msgs=%d reasoning=%s tools=%d", self.model, msg_count, self.reasoning, len(self._tools))
        # 韧性兜底：上游（dashscope 代理/网关）偶发连接级异常（如代理重置、RemoteProtocolError）
        # 会让整个 stream 在首 token 前 1 秒抛错，前端表现为「空气泡」（content 为空）。
        # 这类瞬时错误重试即可恢复，不必透传给用户。仅重试「尚未产生任何 token」的连接级错误；
        # 一旦已吐出 chunk，则保持部分结果、不再重试（避免重复输出）。
        max_attempts = 3
        yielded = False
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            logger.error("LLM_CALL_ERROR status=%d attempt=%d body=%s", response.status_code, attempt, error_text[:500])
                            response.raise_for_status()
                        event_lines: List[str] = []
                        tool_calls_state: List[Dict[str, Any]] = []
                        emitted_indices: set = set()
                        async for line in response.aiter_lines():
                            if line:
                                event_lines.append(line)
                            else:
                                data = self._parse_sse(event_lines)
                                event_lines = []
                                if data:
                                    for chunk in self._process_chunk(data, tool_calls_state, emitted_indices):
                                        yielded = True
                                        yield chunk
                logger.info("LLM_CALL_DONE model=%s attempt=%d", self.model, attempt)
                return
            except httpx.TimeoutException:
                logger.warning("LLM_CALL_TIMEOUT model=%s attempt=%d/%d url=%s", self.model, attempt, max_attempts, url)
                if attempt >= max_attempts:
                    logger.exception("LLM_CALL_TIMEOUT_FATAL model=%s url=%s", self.model, url)
                    raise
            except httpx.HTTPStatusError as e:
                # 4xx/5xx：内容级错误，重试无意义，直接抛。
                logger.exception("LLM_CALL_HTTP_ERROR model=%s attempt=%d error=%s", self.model, attempt, repr(e))
                raise
            except Exception as e:  # 连接级瞬时错误（代理重置等）
                if yielded:
                    logger.warning("LLM_CALL_PARTIAL model=%s attempt=%d (already streamed, stop): %s", self.model, attempt, repr(e))
                    return
                logger.warning("LLM_CALL_RETRY model=%s attempt=%d/%d error_type=%s error_repr=%s",
                               self.model, attempt, max_attempts, type(e).__name__, repr(e))
                if attempt >= max_attempts:
                    logger.exception("LLM_CALL_FATAL model=%s error_type=%s error_repr=%s", self.model, type(e).__name__, repr(e))
                    raise
                await asyncio.sleep(0.5 * attempt)

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        # Non-streaming fallback: collect from sync stream.
        content_parts = []
        reasoning_parts = []
        tool_calls = []
        for chunk in self._stream(messages, stop, run_manager, **kwargs):
            msg = chunk.message
            if msg.content:
                content_parts.append(msg.content)
            rc = (msg.additional_kwargs or {}).get("reasoning_content")
            if rc:
                reasoning_parts.append(rc)
            if getattr(msg, "tool_calls", None):
                tool_calls = msg.tool_calls
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        message = AIMessage(
            content=content,
            tool_calls=tool_calls,
            additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
