"""ApprovalGate —— 工具执行前的"人工裁决"闸门（QwenPaw GovernancePolicy.ASK）。

它不是一个 StopGate（在 governor 里跑、工具已执行后），而是一个"工具前"检查：
- 装在 agent -> tools 之间的 approval 节点；
- 若本轮 AIMessage 的 tool_calls 里有敏感工具，返回 ASK -> 路由到 hitl（interrupt）；
- 否则放行去 tools。

为提升"丝滑"体验（对照 QwenPaw 的信任/放行语义），本门支持：
- **信任模式（trust_mode）**：开启后除"破坏性命令"外一律自动放行，避免编码/agent 任务
  每步都被人工审批打断——这是 Agent Harness 此前"不丝滑"的最致命卡点。
- **安全命令自动放行（auto_approve_safe_exec）**：即使未开信任模式，常见的只读/开发类
  shell 命令（ls/cat/python/npm/git status…）也直接放行；只有写盘、不可逆或危险命令才拦截。
- **破坏性命令硬拦截（DANGEROUS_EXEC）**：无论是否信任模式，rm -rf / mkfs / dd / :(){…} /
  管道到 sh 等一律要求人工裁决，守住底线。
"""
from __future__ import annotations

import re
from typing import Sequence

from langchain_core.messages import AIMessage

from ..gates.base import StopAction, StopHandlerResult

# 默认需要人工确认的敏感工具（可在构造时覆盖）
SENSITIVE_TOOLS = {
    "write_file",
    "edit_file",
    "delete_file",
    "exec",
    "run_command",
    "shell",
    "send_email",
    "publish",
}

# 破坏性命令：即便信任模式也强制要求人工裁决（底线）
DANGEROUS_EXEC = re.compile(
    r"\b("
    r"rm\s+-rf|rm\s+-fr|rm\s+-r\s+-f|"
    r"mkfs|dd\s+if=|shutdown|reboot|halt|poweroff|"
    r":\(\)\s*\{|>\s*/dev/sd|>\s*/dev/nvme|"
    r"curl\s+[^|]*\|\s*(sudo\s+)?(ba)?sh|wget\s+[^|]*\|\s*(ba)?sh|"
    r"chmod\s+-R\s+777|chown\s+-R|"
    r"git\s+push[^&]*--force|git\s+reset\s+--hard|"
    r"sudo\s+rm|sudo\s+dd|"
    r"truncate\s+-s|"
    r"mv\s+/[^ ]*\s+/|"  # 移动进系统根
    r"format\s+"
    r")\b",
    re.IGNORECASE,
)

# 免审批的 exec 命令根：只读 / 查看 / 常见构建与开发工具（不写盘、不可逆）
SAFE_EXEC_ROOTS = {
    "ls", "ll", "cat", "head", "tail", "less", "more", "wc", "nl",
    "grep", "egrep", "fgrep", "rg", "ack", "find", "tree", "locate",
    "pwd", "echo", "printf", "which", "whereis", "whoami", "id", "users",
    "date", "cal", "env", "printenv", "uname", "hostname",
    "df", "du", "free", "top", "htop", "ps", "uptime", "vmstat", "iostat",
    "git", "svn", "hg",
    "python", "python3", "py", "ipython",
    "node", "npm", "npx", "yarn", "pnpm", "bun", "deno",
    "pip", "pip3", "poetry", "conda",
    "cargo", "rustc", "go", "rustup",
    "ruby", "php", "irb", "java", "javac", "scala", "kotlinc", "dotnet",
    "make", "cmake", "ninja", "bazel", "gradle", "mvn",
    "gcc", "g++", "clang", "clang++", "cc", "tcc",
    "sqlite3", "psql", "mysql", "redis-cli",
    "jq", "yq", "awk", "sed", "sort", "uniq", "cut", "paste", "comm", "diff", "patch",
    "xxd", "hexdump", "od", "file", "stat", "readlink", "realpath", "basename", "dirname",
    "open", "screencapture", "say", "osascript", "xdg-open",
    "code", "code-insiders",
}

# 写盘 / 不可逆的 exec 命令根：即便非信任模式也保持谨慎，交给人工裁决
UNSAFE_EXEC_ROOTS = {
    "rm", "mv", "cp", "ln", "touch", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "kill", "pkill", "killall", "sudo", "su", "tee", "dd", "mkfs", "mount", "umount",
    "systemctl", "service", "launchctl", "npm",  # npm 在 SAFE 里是只读查看；这里覆盖写操作
    "git",  # git 在 SAFE 里；写操作(push/commit/reset)由 DANGEROUS 与下方判断兜底
}

# 这些子命令使"本在 safe 根"的命令变成写操作，需要裁决
_WRITE_SUBCOMMANDS = {
    "install", "i", "uninstall", "rm", "remove", "add", "run", "start", "build",
    "deploy", "publish", "link", "cache", "rebuild", "exec", "create", "init",
    "commit", "push", "checkout", "merge", "reset", "clean", "amend", "rebase",
}


def _first_tokens(cmd: str, n: int = 4) -> list[str]:
    """取命令前若干 token（按空白/管道/分号切），用于判断每个子命令的根。"""
    parts = re.split(r"[|;&]+", cmd)
    toks: list[str] = []
    for seg in parts:
        seg = seg.strip()
        if not seg:
            continue
        # 去掉前导 sudo/env 等包装
        words = seg.split()
        if words and words[0] in ("sudo", "env", "time", "nice"):
            words = words[1:]
        if not words:
            continue
        toks.append(words[0])
        # 记录子命令（如 git push 的 push）
        if len(words) > 1:
            toks.append(words[1])
        if len(toks) >= n:
            break
    return toks


class ApprovalGate:
    """返回 StopHandlerResult 形态，便于和 run_stop_gates 体系衔接（虽在独立节点用）。

    trust_mode: 信任模式。开启后除破坏性命令外全部自动放行。
    auto_approve_safe_exec: 安全命令自动放行（默认开）。仅当 trust_mode 关闭时生效。
    """

    def __init__(
        self,
        sensitive: Sequence[str] = None,
        priority: int = 5,
        trust_mode: bool = False,
        auto_approve_safe_exec: bool = True,
    ):
        self.sensitive = set(sensitive) if sensitive else set(SENSITIVE_TOOLS)
        self.priority = priority
        self.trust_mode = trust_mode
        self.auto_approve_safe_exec = auto_approve_safe_exec

    def _is_sensitive(self, name: str) -> bool:
        return name in self.sensitive or any(
            name.endswith("__" + s) for s in self.sensitive
        )

    def _is_dangerous_exec(self, command: str) -> bool:
        return bool(DANGEROUS_EXEC.search(command or ""))

    def _is_safe_exec(self, command: str) -> bool:
        """判断一条 exec 命令是否可被自动放行（只读/查看/常见开发工具，无写盘）。"""
        if not command or not command.strip():
            return False
        toks = _first_tokens(command)
        if not toks:
            return False
        root = toks[0]
        if root not in SAFE_EXEC_ROOTS:
            return False
        # 对带子命令的工具（git/npm 等）检查是否触发写操作
        if len(toks) > 1:
            sub = toks[1]
            if sub in _WRITE_SUBCOMMANDS:
                return False
        return True

    def check(self, state: dict) -> "StopHandlerResult | None":
        last = state.get("messages", [])[-1] if state.get("messages") else None
        if not isinstance(last, AIMessage):
            return None
        calls = getattr(last, "tool_calls", None) or []

        flagged = [c["name"] for c in calls if self._is_sensitive(c["name"])]
        if not flagged:
            return None

        # 逐条评估，分离"仍需裁决"与"可自动放行"
        still_need_approval: list[str] = []

        for c in calls:
            name = c["name"]
            if not self._is_sensitive(name):
                continue

            if name == "exec":
                command = (c.get("args") or {}).get("command", "") or ""
                # 破坏性命令：任何模式都拦（底线）
                if self._is_dangerous_exec(command):
                    still_need_approval.append(f"{name}(危险命令)")
                    continue
                # 信任模式：放行其余 exec
                if self.trust_mode:
                    continue
                # 非信任但安全命令自动放行
                if self.auto_approve_safe_exec and self._is_safe_exec(command):
                    continue
                # 其余 exec（写盘/不可逆/未知）需裁决
                still_need_approval.append(name)
                continue

            # 非 exec 的敏感工具（write_file/edit_file/…）
            if self.trust_mode:
                continue  # 信任模式下全部放行
            still_need_approval.append(name)

        if still_need_approval:
            return StopHandlerResult(
                action=StopAction.ASK,
                reason=f"需要人工确认的操作: {', '.join(still_need_approval)}",
            )
        return None
