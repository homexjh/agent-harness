"""agent 节点：把 LLM 包装成 LangGraph 可识别的 ChatModel 节点。

- 继承 BaseChatModel，让 LangGraph 在 stream_mode="messages" 下直接捕获 token 级流式事件，
  从而前端能像 QwenPaw 一样实时看到思考和答案逐字出现。
- 非流式调用（ainvoke/invoke）保留重试 + 错误降级，避免配置错误时空等。
- 流式调用（astream/_astream）直接代理底层模型流式输出，首 token 尽快到达前端。
- 将 Responses API 风格的 reasoning 块解析成可读思考文本，放入 additional_kwargs。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import Field

from .stability.retry import TransientError
from .stability.observability import log_event


def _extract_reasoning_text(content_list: list) -> str:
    """从 OpenAI Responses API 风格的 reasoning 块中提取可读的思考文本。"""
    parts = []
    for block in content_list:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "reasoning":
            for summary in block.get("summary", []):
                if isinstance(summary, dict) and summary.get("type") == "summary_text":
                    parts.append(summary.get("text", ""))
        elif block.get("type") == "thinking" and block.get("thinking"):
            parts.append(block.get("thinking"))
    return "".join(parts)


def _extract_answer_text(content_list: list) -> str:
    """从 OpenAI Responses API 风格的内容块中提取最终答案文本。"""
    parts = []
    for block in content_list:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _normalize_chunk(chunk: BaseMessage) -> BaseMessage:
    """把 Responses API 风格的列表 content 转成普通字符串，同时保留 reasoning_content。"""
    content = getattr(chunk, "content", None)
    if not isinstance(content, list):
        return chunk

    reasoning = _extract_reasoning_text(content)
    answer = _extract_answer_text(content)
    additional = dict(getattr(chunk, "additional_kwargs", {}) or {})
    if reasoning:
        additional["reasoning_content"] = reasoning

    # 直接修改 chunk 的 content 和 additional_kwargs 可能会破坏合并语义，
    # 因此构造一个新的 AIMessageChunk。
    return AIMessageChunk(content=answer, additional_kwargs=additional)


def _classify(model_err: Exception) -> Exception:
    # 把常见瞬时故障归类为 TransientError 以便重试
    msg = str(model_err).lower()
    if any(k in msg for k in ("timeout", "timed out", "429", "rate limit", "503", "502", "500", "connection")):
        return TransientError(str(model_err))

    # 配置/参数类错误：API 不接受某个参数、模型不存在、key 格式错……
    # 重试没有帮助，直接快速失败，避免用户空等。
    fatal_keywords = (
        "unexpected keyword argument",
        "invalid",
        "not supported",
        "not found",
        "unknown parameter",
        "bad request",
        "invalid model",
        "model not found",
        "unsupported",
    )
    if isinstance(model_err, TypeError) or any(k in msg for k in fatal_keywords):
        return RuntimeError(f"[配置/参数错误，不重试] {model_err}")

    return model_err


class AgentNode(BaseChatModel):
    """LangGraph 的 agent 节点：包装 LLM 并绑定工具。

    - 作为 BaseChatModel 被 LangGraph 识别，stream_mode="messages" 能直接捕获
      token 级流式事件，前端可实时看到思考和答案。
    - 非流式调用带重试/错误降级；流式调用直接代理底层模型，不二次包装。
    """

    model: Any = Field(...)
    tools: list = Field(default_factory=list)
    metrics: Optional[Any] = Field(default=None)
    breaker: Optional[Any] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "agent-node"

    def _extract_messages(self, input_value: Any) -> list:
        """LangGraph 作为普通 Runnable 会把整个 state dict 传进来；从中提取真正的 messages。"""
        if isinstance(input_value, dict):
            return input_value.get("prompt_messages") or input_value.get("messages", [])
        if isinstance(input_value, list):
            return input_value
        if input_value is None:
            return []
        # 单条字符串 / PromptValue 等交给 BaseChatModel 的标准转换
        return input_value

    def invoke(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[list] = None,
        **kwargs: Any,
    ) -> AIMessage:
        messages = self._extract_messages(input)
        return super().invoke(messages, config=config, stop=stop, **kwargs)

    async def ainvoke(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[list] = None,
        **kwargs: Any,
    ) -> AIMessage:
        messages = self._extract_messages(input)
        return await super().ainvoke(messages, config=config, stop=stop, **kwargs)

    def stream(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[list] = None,
        **kwargs: Any,
    ) -> Iterator[AIMessageChunk]:
        messages = self._extract_messages(input)
        yield from super().stream(messages, config=config, stop=stop, **kwargs)

    async def astream(
        self,
        input: Any,
        config: Optional[RunnableConfig] = None,
        *,
        stop: Optional[list] = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        messages = self._extract_messages(input)
        async for chunk in super().astream(messages, config=config, stop=stop, **kwargs):
            yield chunk

    def _bind_tools(self):
        if self.tools and hasattr(self.model, "bind_tools"):
            return self.model.bind_tools(self.tools)
        return self.model

    def _generate(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        """同步非流式生成（带重试/降级）。"""
        bound = self._bind_tools()
        response = self._call_with_retry_sync(bound, messages)
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        """异步非流式生成（带重试/降级）。"""
        bound = self._bind_tools()
        response = await self._call_with_retry(bound, messages)
        return ChatResult(generations=[ChatGeneration(message=response)])

    def _stream(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> Iterator[ChatGenerationChunk]:
        """同步流式生成：直接代理底层模型流式输出。"""
        bound = self._bind_tools()
        for raw_chunk in bound.stream(messages):
            chunk = _normalize_chunk(raw_chunk)
            yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """异步流式生成：直接代理底层模型流式输出。

        发送增量 reasoning 片段——前端 SDK 的 concat 会自动追加字符串，
        因此不要在后端累积，否则会导致重复文本。
        """
        bound = self._bind_tools()
        async for raw_chunk in bound.astream(messages):
            chunk = _normalize_chunk(raw_chunk)
            yield ChatGenerationChunk(message=chunk)

    def bind_tools(self, tools, *args, **kwargs):
        """返回一个绑定新工具的 AgentNode 副本。"""
        return AgentNode(
            model=self.model,
            tools=list(tools),
            metrics=self.metrics,
            breaker=self.breaker,
        )

    def _call_with_retry_sync(self, bound, messages):
        """同步重试调用：仅用于非流式 invoke。"""
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                return bound.invoke(messages)
            except Exception as e:  # noqa: BLE001
                classified = _classify(e)
                if isinstance(classified, TransientError):
                    last_err = classified
                    time.sleep(min(2 ** attempt, 4))
                else:
                    break
        if last_err is not None:
            raise last_err
        # 兜底：再试一次不带重试，让原始错误暴露
        return bound.invoke(messages)

    async def _call_with_retry(self, bound, messages):
        """异步重试调用：仅用于非流式 ainvoke。"""
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                return await bound.ainvoke(messages)
            except Exception as e:  # noqa: BLE001
                classified = _classify(e)
                if isinstance(classified, TransientError):
                    last_err = classified
                    await asyncio.sleep(min(2 ** attempt, 4))
                else:
                    break
        if last_err is not None:
            raise last_err
        # 兜底：再试一次不带重试，让原始错误暴露
        return await bound.ainvoke(messages)


def make_call_model(model, *, tools=None, metrics=None, breaker=None):
    """兼容旧名的工厂函数，返回 AgentNode ChatModel。"""
    return AgentNode(
        model=model,
        tools=list(tools) if tools else [],
        metrics=metrics,
        breaker=breaker,
    )
