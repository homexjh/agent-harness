"""ToolGuardEngine —— 工具调用的防御纵深（移植自 QwenPaw security/tool_guard/engine.py）。

设计：
- 每个 Guardian 只看 (tool_name, args)，返回 GuardResult(allowed, reason)。
- Engine 聚合多个 guardian：任一拒绝即拒绝（deny-by-default 之外的"显式否决"）。
- guardian 自身抛异常被捕获，不阻塞主流程（安全失败取向：宁可拒）。

默认三守卫：
- FilePathGuardian：阻止路径穿越 / 写出允许根目录之外。
- RuleBasedGuardian：按规则名/参数模式拦截高危操作（rm -rf、format 等）。
- ShellEvasionGuardian：检测参数里的 shell 元字符注入（; && $() 等）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> "GuardResult":
        return cls(True, reason)

    @classmethod
    def deny(cls, reason: str) -> "GuardResult":
        return cls(False, reason)


class BaseGuardian:
    name = "base"

    def check(self, tool_name: str, args: dict) -> GuardResult:
        return GuardResult.allow()


class FilePathGuardian(BaseGuardian):
    name = "FilePath"

    def __init__(self, allowed_roots: Optional[Sequence[str]] = None):
        # 允许根：不传则默认当前工作目录；传了则严格限制在这几棵树内
        self.allowed_roots = [os.path.abspath(r) for r in (allowed_roots or ["."])]

    def check(self, tool_name: str, args: dict) -> GuardResult:
        path_fields = [v for k, v in (args or {}).items() if "path" in k.lower() or "file" in k.lower()]
        for raw in path_fields:
            if not isinstance(raw, str):
                continue
            try:
                ap = os.path.abspath(os.path.expanduser(raw))
            except Exception:
                return GuardResult.deny(f"{self.name}: cannot resolve path '{raw}'")
            if not any(ap == root or ap.startswith(root + os.sep) for root in self.allowed_roots):
                return GuardResult.deny(
                    f"{self.name}: path '{raw}' escapes allowed roots {self.allowed_roots}"
                )
        return GuardResult.allow()


class RuleBasedGuardian(BaseGuardian):
    name = "RuleBased"

    # 工具名 -> 必须"显式 opt-in"才放行的危险操作
    DANGEROUS_TOOLS = {
        "delete_file",
        "remove_file",
        "format_disk",
        "drop_table",
        "exec",
        "run_command",
        "shell",
    }

    def __init__(self, allowlist: Optional[set] = None):
        self.allowlist = allowlist or set()

    def check(self, tool_name: str, args: dict) -> GuardResult:
        if tool_name in self.DANGEROUS_TOOLS and tool_name not in self.allowlist:
            return GuardResult.deny(f"{self.name}: tool '{tool_name}' is blocklisted")
        # 参数里的明文危险命令
        blob = " ".join(str(v) for v in (args or {}).values()).lower()
        for pat in ("rm -rf", "rm -r -f", "mkfs", "dd if=", ":(){", "drop database"):
            if pat in blob:
                return GuardResult.deny(f"{self.name}: dangerous pattern '{pat}' in args")
        return GuardResult.allow()


class ShellEvasionGuardian(BaseGuardian):
    name = "ShellEvasion"

    _INJECTION = re.compile(r"(;|\|\||\&\&|\$\(|\`|>\s*/|\|\s*)")

    def check(self, tool_name: str, args: dict) -> GuardResult:
        for v in (args or {}).values():
            if not isinstance(v, str):
                continue
            hits = self._INJECTION.findall(v)
            if hits:
                return GuardResult.deny(
                    f"{self.name}: shell injection metachars {hits} in args"
                )
        return GuardResult.allow()


class ToolGuardEngine:
    """聚合多个 guardian。默认开三守卫；异常被吞掉并记为拒绝（fail-closed）。"""

    def __init__(
        self,
        guardians: Optional[Sequence[BaseGuardian]] = None,
        policy: str = "deny",
    ):
        self.policy = policy
        self.guardians = list(guardians) if guardians else [
            FilePathGuardian(),
            RuleBasedGuardian(),
            ShellEvasionGuardian(),
        ]

    def evaluate(self, tool_name: str, args: dict) -> GuardResult:
        reasons = []
        for g in self.guardians:
            try:
                r = g.check(tool_name, args)
            except Exception as e:  # fail-closed
                return GuardResult.deny(f"{g.name}: guardian crashed ({e})")
            if not r.allowed:
                return r
            if r.reason:
                reasons.append(r.reason)
        return GuardResult.allow("; ".join(reasons))

    def is_allowed(self, tool_name: str, args: dict) -> bool:
        return self.evaluate(tool_name, args).allowed
