"""治理与安全包（防御纵深）。

对照 QwenPaw：
- ToolGuardEngine 懒单例，默认 3 guardian（FilePath / RuleBased / ShellEvasion）；
- PolicyGuardedTool 把任意工具包一层守卫，拒绝执行而非放行；
- ApprovalGate 在"工具执行前"触发 ASK -> interrupt() 真人工裁决。
"""
from .guard import (
    BaseGuardian,
    FilePathGuardian,
    RuleBasedGuardian,
    ShellEvasionGuardian,
    GuardResult,
    ToolGuardEngine,
)
from .guarded_tool import PolicyGuardedTool, make_guarded_tools
from .approval import ApprovalGate, SENSITIVE_TOOLS

__all__ = [
    "BaseGuardian",
    "FilePathGuardian",
    "RuleBasedGuardian",
    "ShellEvasionGuardian",
    "GuardResult",
    "ToolGuardEngine",
    "PolicyGuardedTool",
    "make_guarded_tools",
    "ApprovalGate",
    "SENSITIVE_TOOLS",
]
