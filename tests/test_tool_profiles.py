"""按 mode 给工具面（tool profile）测试。

设计（Option C：chat 默认 + 反应式意图升级）：
- 闲聊（chat）模式**保留**重工具（exec / write_file / edit_file / desktop_screenshot）
  在 schema 中，但由 ModeEscalationGuardian 在调用时反应式放行 + 升级到 coding；
  不再物理删除（否则正则漏判时任务静默做不动）。
- 其余模式（coding / mission）全开，保持旧行为。
- 用户手动锁定 chat（auto_mode=False）时，守卫改为拒绝重工具（见 test_mode_escalation.py）。
"""
from __future__ import annotations

import tempfile

from src.server import graph_provider as gp


def _tool_names(mode: str | None = None):
    kwargs = {"workdir": tempfile.mkdtemp(), "cm": gp.get_context_manager()}
    if mode is not None:
        kwargs["mode"] = mode
    tools = gp._build_tools(**kwargs)
    return set(tools.keys())


def test_chat_profile_keeps_heavy_tools_guarded():
    # chat 不再物理删除重工具——必须由守卫在调用时反应式升级，否则无法"面面具到"
    names = _tool_names("chat")
    for heavy in ("exec", "write_file", "edit_file", "desktop_screenshot"):
        assert heavy in names, f"chat 应保留（守卫托管）重工具 {heavy}"
    # 闲聊应保留的轻交互工具
    for light in (
        "read_file", "list_dir", "get_current_time", "calculator",
        "web_search", "view_image", "view_video", "create_timer",
        "recall", "memory_search",
    ):
        assert light in names, f"chat profile 应保留 {light}"


def test_default_mode_is_chat():
    # 不传 mode 应等价于 chat（向后兼容旧的两参调用，如 test_scheduler）
    assert _tool_names(None) == _tool_names("chat")


def test_mission_profile_keeps_full_tools():
    names = _tool_names("mission")
    for heavy in ("exec", "write_file", "edit_file", "desktop_screenshot"):
        assert heavy in names, f"mission profile 应保留 {heavy}"


def test_coding_profile_keeps_full_tools():
    names = _tool_names("coding")
    for heavy in ("exec", "write_file", "edit_file", "desktop_screenshot"):
        assert heavy in names, f"coding profile 应保留 {heavy}"


def test_unknown_mode_falls_back_to_full():
    # 未知 mode 不应裁剪（避免误伤）
    names = _tool_names("some_unknown_mode_xyz")
    for heavy in ("exec", "write_file", "edit_file", "desktop_screenshot"):
        assert heavy in names, f"未知 mode 应保留 {heavy}"


def test_request_graph_passes_mode_into_tools():
    """get_request_graph 已知 mode，应把 mode 透传给 _build_tools。

    用 monkeypatch 捕获实际传入的 mode，并短路 build_graph 避免触发完整图构建
    （完整构建需要 checkpointer / 模型等测试环境未就绪的资源）。mode 在 build_graph
    之前已被 _build_tools 捕获，因此短路不影响断言。
    """
    import src.server.graph_provider as gpmod
    from src.server.graph_provider import get_request_graph

    captured = {}

    def fake_build_tools(workdir, cm, filter_disabled=True, mode="chat"):
        captured["mode"] = mode
        return original(workdir, cm, filter_disabled=filter_disabled, mode=mode)

    def fake_build_graph(*a, **k):
        return object()  # 短路：不需要真实图

    def fake_get_checkpointer():
        return None  # 短路：不需要真实 checkpointer

    original = gpmod._build_tools
    orig_get_checkpointer = gpmod.get_shared_checkpointer
    gpmod._build_tools = fake_build_tools
    gpmod.build_graph = fake_build_graph
    gpmod.get_shared_checkpointer = fake_get_checkpointer
    try:
        get_request_graph({"mode": "chat", "model": "deepseek-chat"})
        assert captured.get("mode") == "chat"
        get_request_graph({"mode": "mission", "model": "deepseek-chat"})
        assert captured.get("mode") == "mission"
    finally:
        gpmod._build_tools = original
        del gpmod.build_graph
        gpmod.get_shared_checkpointer = orig_get_checkpointer
