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

    # 文件写入/编辑类工具的"数据载荷"字段：内容是代码/文本/HTML，绝不是 shell
    # 命令。若对其扫描 shell 元字符，任何 JS/HTML 文件都会因分号/管道被误杀。
    # 这些工具由 FilePathGuardian 限制在 workspace 根内，安全由沙箱保证，故
    # ShellEvasion 完全跳过其数据字段（对齐 QwenPaw/WorkBuddy「只扫命令串」）。
    _DATA_FIELDS = {
        "content", "old", "old_string", "new", "new_string",
        "text", "code", "code_text", "source", "data", "body", "script", "html",
    }
    # shell 类工具：只扫命令串（且这些工具在图里本就走不带 ShellEvasion 的 exec 引擎）。
    _EXEC_TOOLS = {"exec", "run_command", "shell", "bash", "terminal"}
    _EXEC_FIELDS = {"command", "cmd"}

    def check(self, tool_name: str, args: dict) -> GuardResult:
        if tool_name in self._EXEC_TOOLS:
            fields = [v for k, v in (args or {}).items() if k in self._EXEC_FIELDS]
        else:
            # 非 shell 工具：跳过数据载荷字段，仅扫描路径/参数等控制字段
            fields = [v for k, v in (args or {}).items() if k not in self._DATA_FIELDS]
        for v in fields:
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


# —— 反应式意图升级（Option C：chat 默认 + 重工具意图自动升级）——
# 思路：不要让后端去"读用户文本猜意图"（正则必漏），而是让"模型自己想调什么工具"
# 来暴露任务性质。chat 模式下重工具 **保留在 schema 里但包一层守卫**：模型一旦尝试调用，
# 守卫放行执行，并同时把会话升到 coding（通过 per-request 字典回传 app.py 下发
# mode_escalate 事件，前端同步锁定）。无论用户怎么措辞，只要任务真需要重工具必被捕获。
#
# 并发安全：tools_node 在调用工具前用 set_escalation_thread(tid) 写入当前线程 id，
# 守卫在本次同步 invoke 内读取——本地单用户 harness 下无交叉污染。
import threading as _threading  # noqa: E402

_CURRENT_TID: str | None = None
_mode_by_thread: dict = {}
_locked_by_thread: dict = {}
_esc_by_thread: dict = {}  # tid -> 升级目标模式（"coding"）
_esc_lock = _threading.Lock()


def set_escalation_thread(tid: str | None) -> None:
    global _CURRENT_TID
    _CURRENT_TID = tid


def set_request_mode(tid: str, mode: str) -> None:
    """请求开始时由 app.py 写入该线程的当前模式（chat/coding/mission）。"""
    _mode_by_thread[tid] = (mode or "chat").strip().lower()


def set_request_locked(tid: str, locked: bool) -> None:
    """请求开始时由 app.py 写入该线程是否被用户手动锁定（auto_mode=False）。"""
    _locked_by_thread[tid] = bool(locked)


def pop_escalation(tid: str):
    """图流结束后由 app.py 抽取升级信号（有则返回目标模式，无则返回 None）。"""
    with _esc_lock:
        return _esc_by_thread.pop(tid, None)


def clear_request(tid: str) -> None:
    """请求结束（含异常）由 app.py 清理 per-request 状态，避免泄漏。"""
    _mode_by_thread.pop(tid, None)
    _locked_by_thread.pop(tid, None)
    _esc_by_thread.pop(tid, None)


class ModeEscalationGuardian(BaseGuardian):
    """反应式意图升级守卫：chat 模式下模型尝试重工具时，放行执行并升级会话到 coding。

    - mode != chat（coding/mission）：直接放行，不做任何事（保持旧行为）。
    - mode == chat 且用户手动锁定（auto_mode=False）：尊重用户选择，拒绝重工具，
      提示用 /mode coding 切换。
    - mode == chat 且未锁定：放行执行，同时把会话标记为 coding（供 app.py 下发升级事件），
      使后续同轮 / 后续请求稳定以 coding 全工具运行。
    """

    HEAVY_TOOLS = {"exec", "write_file", "edit_file", "desktop_screenshot"}

    def check(self, tool_name: str, args: dict) -> "GuardResult":
        tid = _CURRENT_TID
        mode = _mode_by_thread.get(tid, "chat") if tid else "chat"
        if mode != "chat":
            return GuardResult.allow()
        if tool_name not in self.HEAVY_TOOLS:
            return GuardResult.allow()
        if tid and _locked_by_thread.get(tid, False):
            return GuardResult.deny(
                "该操作需要 coding 模式；当前会话已锁定为 chat，请用 /mode coding 切换后重试。"
            )
        # 反应式升级：放行执行，同时把会话升到 coding
        if tid:
            _mode_by_thread[tid] = "coding"
            with _esc_lock:
                _esc_by_thread[tid] = "coding"
        return GuardResult.allow("已自动从 chat 升级到 coding 模式以执行该操作")
