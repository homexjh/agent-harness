"""对话轨（chat）工具 profile 与轻量提示词测试。

验证「对话轨」设计：chat 模式采用「显式 allowlist」只放行「读 + 联网」轻工具，
其余工具（含将来新增的带副作用工具）一律不进入 chat；coding / mission 仍全开。
同时验证 chat 系统提示不含任务执行指令，且 chat 不写长期记忆（auto_memory）。
"""
from __future__ import annotations

import tempfile

from src.server import graph_provider as gp
from src.harness import agentmode
from src.harness.graph import _writes_long_term_memory


def _tool_names(mode: str | None = None):
    kwargs = {"workdir": tempfile.mkdtemp(), "cm": gp.get_context_manager()}
    if mode is not None:
        kwargs["mode"] = mode
    tools = gp._build_tools(**kwargs)
    return set(tools.keys())


def test_chat_keeps_read_and_online_tools():
    names = _tool_names("chat")
    for light in (
        "read_file", "list_dir", "get_current_time", "calculator",
        "web_search", "view_image", "view_video", "recall", "memory_search",
    ):
        assert light in names, f"chat 应保留轻工具 {light}"


def test_chat_disables_heavy_tools():
    names = _tool_names("chat")
    for heavy in ("write_file", "edit_file", "exec", "desktop_screenshot", "create_timer"):
        assert heavy not in names, f"chat 不应含重工具 {heavy}"


def test_default_mode_is_chat():
    assert _tool_names(None) == _tool_names("chat")


def test_coding_keeps_full_tools():
    names = _tool_names("coding")
    for heavy in ("write_file", "edit_file", "exec", "desktop_screenshot", "create_timer"):
        assert heavy in names, f"coding 应保留重工具 {heavy}"


def test_mission_keeps_full_tools():
    names = _tool_names("mission")
    for heavy in ("write_file", "edit_file", "exec", "desktop_screenshot", "create_timer"):
        assert heavy in names, f"mission 应保留重工具 {heavy}"


def test_chat_prompt_is_lightweight():
    """chat 系统提示不应包含 _CORE 的任务执行指令（动手/闭环/工具纪律等）。"""
    p = agentmode.CHAT_PROMPT
    # 这些只应出现在 coding/mission 的 _CORE 任务提示里，chat 必须缺席
    for task_instruction in (
        "先动手，再开口",
        "闭环交付",
        "工具纪律",
        "exec 在工作区内执行 shell",
        "改文件前先读原文",
    ):
        assert task_instruction not in p, f"chat 提示不应含任务执行指令：{task_instruction}"
    # chat 必须明确表达「只读 + 联网」的对话定位
    assert "对话模式" in p
    assert "联网" in p
    # coding / mission 提示仍保留任务执行指令（回归保护）
    assert "编码模式（coding）" in agentmode.CODING_PROMPT
    assert "用工具读/写/改文件" in agentmode.CODING_PROMPT
    assert "任务模式（mission）" in agentmode.MISSION_PROMPT
    assert "MISSION COMPLETE" in agentmode.MISSION_PROMPT


def test_chat_toolset_exactly_matches_allowlist():
    """chat 工具面必须由 allowlist 精确决定：不多不少，恰好那 9 个只读/联网工具。

    这条测试锁定「chat = 只读」的不变量——将来新增带副作用工具（如 send_email /
    delete_file）也不会静默漏进 chat，因为未显式列出的工具一律被剔除。
    """
    expected = {
        "calculator", "read_file", "list_dir", "view_image", "view_video",
        "get_current_time", "web_search", "recall", "memory_search",
    }
    assert _tool_names("chat") == expected


def test_chat_skips_long_term_memory_write():
    """对话轨（chat）不写长期记忆；coding / mission 写。

    直接锁定 `graph._writes_long_term_memory` 这一不变量——context_node 据此跳过
    auto_memory，使 chat 不落记忆（也规避了同步写记忆卡 60s+ 的老问题）。
    """
    assert _writes_long_term_memory("chat") is False
    assert _writes_long_term_memory("Chat") is False  # 大小写不敏感
    assert _writes_long_term_memory("coding") is True
    assert _writes_long_term_memory("mission") is True
    assert _writes_long_term_memory(None) is False  # 默认按 chat 处理 → 不写


def test_chat_agent_model_has_output_token_cap():
    """对话轨的 agent 模型必须注入 max_tokens 上限，阻止闲聊无限铺陈。

    对应根因修复：此前 chat 不设 max_tokens，「你是谁」曾吐 846 chunk / 107s。
    coding / mission 不应被限制（保持旧行为，可长文产出）。
    """
    from src.harness.agent import make_call_model
    from src.harness.graph import CHAT_MAX_TOKENS

    # 用一个假的 ChatOpenAI-like 对象占位 model（只需被 bind_tools/bind 调用）
    class _FakeModel:
        def bind_tools(self, tools, **kwargs):
            return _FakeBound(kwargs)
        def bind(self, **kwargs):
            return _FakeBound(kwargs)

    class _FakeBound:
        def __init__(self, kwargs):
            self.kwargs = kwargs

    model = _FakeModel()
    chat_node = make_call_model(model, tools=[], max_tokens=CHAT_MAX_TOKENS)
    bound = chat_node._bind_tools()
    assert isinstance(bound, _FakeBound), "chat 应把 max_tokens 注入绑定"
    assert bound.kwargs.get("max_tokens") == CHAT_MAX_TOKENS, "chat 必须注入 max_tokens 上限"

    # coding / mission 不该限制
    coding_node = make_call_model(model, tools=[], max_tokens=None)
    bound_c = coding_node._bind_tools()
    assert getattr(bound_c, "kwargs", {}) == {} or bound_c.kwargs.get("max_tokens") is None
