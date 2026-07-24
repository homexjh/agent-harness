"""对话轨（chat）工具 profile 与轻量提示词测试。

验证「对话轨」设计：chat 模式只保留「读 + 联网」轻工具，关闭写文件/改文件/
执行命令/截屏/定时等重工具；coding / mission 仍全开。同时验证 chat 系统提示
不含任务执行指令。
"""
from __future__ import annotations

import tempfile

from src.server import graph_provider as gp
from src.harness import agentmode


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
