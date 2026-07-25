"""模型工厂 —— DeepSeek（OpenAI 兼容）即开即用，env 驱动。

验证方式：
- 真实跑：设置 DEEPSEEK_API_KEY（+ 可选 DEEPSEEK_BASE_URL）后调用 make_deepseek_model()；
- 无 key 时：用 make_scripted_model() 做确定性全链路验证（详见 examples/run_stable.py）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _reasoning_kwargs(model: str, provider: Optional[str] = None) -> Dict[str, Any]:
    """按厂商和模型名返回正确的 reasoning 额外参数。

    空字典表示：该模型名本身已决定推理能力，不需要额外参数。
    这是为了避免把 ``thinking=true`` 这种非通用参数塞给不支持的模型，
    导致 ``Completions.create() got an unexpected keyword argument 'thinking'``。
    """
    low = (model or "").lower()
    pid = (provider or "").lower()

    # OpenAI o 系列：官方参数 reasoning_effort
    if "o1" in low or "o3" in low:
        return {"reasoning_effort": "high"}

    # DeepSeek：推理能力由模型名决定（deepseek-reasoner）。
    # DeepSeek 的 OpenAI 兼容接口不接受 ``thinking`` 字段。
    if pid == "deepseek" or "deepseek" in low:
        return {}

    # 阿里云百炼 Qwen3 / QwQ：OpenAI 兼容接口用 reasoning={"type": "enabled"}
    # （传 thinking=true 会被拒绝，要求 json_object）
    if pid == "qwen" and ("qwen3" in low or "qwq" in low):
        return {"reasoning": {"type": "enabled"}}

    # 智谱 GLM-Z1 系列：模型名决定推理能力
    if pid == "zhipu" and "z1" in low:
        return {}

    # 默认：不注入未知参数，避免把请求打挂
    return {}


def _no_thinking_kwargs(
    model: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """非推理模式下，对默认开启 thinking 的厂商显式关闭。

    实测（dashscope 兼容模式 qwen3.6-plus）：不传时接口默认 thinking=ON，
    每次闲聊会在后台生成大量隐藏推理 token，首字与总耗时翻 ~3 倍
    （同模型 QwenPaw 显式 enable_thinking=false 约 3s，agent-harness 现网约 10s）。
    对齐 QwenPaw 行为，闲聊/普通对话关掉它。
    """
    low = (model or "").lower()
    pid = (provider or "").lower()
    bu = (base_url or "").lower()
    if pid == "qwen" or "dashscope" in bu or "qwen3" in low or "qwq" in low:
        return {"enable_thinking": False}
    return {}


def _is_qwen_dashscope(
    model: str,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> bool:
    """判断是否为 dashscope/通义 qwen（默认 thinking=ON，需要自定义模型控制 enable_thinking）。"""
    low = (model or "").lower()
    pid = (provider or "").lower()
    bu = (base_url or "").lower()
    return pid == "qwen" or "dashscope" in bu or "qwen3" in low or "qwq" in low


def make_deepseek_model(
    model: str = "deepseek-v4-pro",
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: float = 0.3,
    reasoning: bool = False,
    provider: Optional[str] = None,
    **kwargs,
):
    """返回一个绑定到 DeepSeek（OpenAI 兼容）的 ChatOpenAI 客户端。

    同时适用于任意 OpenAI 兼容厂商（OpenAI / Qwen / 智谱 / Kimi / 硅基流动 / Ollama…），
    只要传入对应 base_url + api_key 即可。

    reasoning：开启「思考/推理」模式。具体行为按厂商差异化：
    - DeepSeek 系：选 ``deepseek-reasoner`` 模型名即可，不额外传参；
    - OpenAI o 系列（o1/o3）：传 ``reasoning_effort=high``；
    - Qwen3 / QwQ：传 ``reasoning={"type": "enabled"}``；
    - 其他：保守处理，不注入未知参数。
    开启时 temperature 强制为 1.0（推理模型通常要求）。
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "langchain-openai 未安装，请 `pip install langchain-openai` 后重试"
        ) from e

    api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未设置：请 export DEEPSEEK_API_KEY=... 或传入 api_key="
        )
    base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

    model_kwargs: Dict[str, Any] = {}
    reasoning_payload: Optional[Dict[str, Any]] = None
    if reasoning:
        reasoning_kwargs = _reasoning_kwargs(model, provider)
        # OpenAI 官方建议 reasoning_effort 作为显式参数，避免 model_kwargs 警告
        if "reasoning_effort" in reasoning_kwargs:
            kwargs["reasoning_effort"] = reasoning_kwargs.pop("reasoning_effort")
        # Qwen3 / QwQ 的 reasoning 是 ChatOpenAI 顶层参数
        if "reasoning" in reasoning_kwargs:
            reasoning_payload = reasoning_kwargs.pop("reasoning")
        model_kwargs = reasoning_kwargs
    else:
        # 非推理模式：对默认 thinking=ON 的厂商（qwen/dashscope）显式关闭，
        # 否则每次闲聊都在后台做隐藏推理，耗时翻数倍。
        model_kwargs = _no_thinking_kwargs(model, provider, base_url)

    chat_kwargs: Dict[str, Any] = dict(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=(1.0 if reasoning else temperature),
        model_kwargs=model_kwargs,
        # 显式超时，避免 dashscope/厂商接口偶发慢响应时后端干等（openai SDK 默认 600s）。
        # 与 reasoning 分支的 httpx.Timeout(120.0) 保持一致。
        timeout=120,
        max_retries=2,
        **kwargs,
    )
    if reasoning_payload is not None:
        chat_kwargs["reasoning"] = reasoning_payload

    # reasoning 模式需要捕获 reasoning_content；openai 2.x 尚未暴露该字段，
    # 因此使用基于 httpx 的自定义模型，其余场景保持 ChatOpenAI 以获得最大兼容性。
    if reasoning or _is_qwen_dashscope(model, provider, base_url):
        from .reasoning_model import ReasoningChatOpenAI
        return ReasoningChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=(1.0 if reasoning else temperature),
            reasoning=reasoning,
        )

    return ChatOpenAI(**chat_kwargs)


class ScriptedModel:
    """确定性脚本模型：按脚本逐步返回 AIMessage / 带 tool_calls 的 AIMessage。

    用于无 API key 的全链路稳定性验证（可控、可断言、可复现）。
    """

    def __init__(self, script: list):
        # script: list of (callable(state)->AIMessage) 或 直接 AIMessage
        self._script = script
        self._i = 0

    def invoke(self, messages):
        if self._i >= len(self._script):
            # 脚本用尽：默认给一个收尾答案，避免空转
            from langchain_core.messages import AIMessage

            return AIMessage(content="[script exhausted] final answer.")
        item = self._script[self._i]
        self._i += 1
        if callable(item):
            return item(messages)
        return item

    @property
    def step(self) -> int:
        return self._i

    async def ainvoke(self, messages, *args, **kwargs):
        return self.invoke(messages)
