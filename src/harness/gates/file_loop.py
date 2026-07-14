"""FileLoopGate（QwenPaw: loop/gates/file_loop.py，Ralph/Ultrawork 长任务循环控制）。

文件驱动的持久化循环闸门——以磁盘上的"状态目录/完成标记文件"作为真相源，
比纯内存状态健壮（进程重启可续跑）。

- 完成哨兵文件存在（如 loop_dir/DONE 或 status.json 里 done=true）-> STOP；
- 否则 CONTINUE，附带剩余任务提示，把 Agent 钉在任务轨道上；
- max_turns 硬上限兜底，避免文件永不出现时无限打转。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .base import StopGate, StopHandlerResult, StopAction


class FileLoopGate(StopGate):
    name = "file_loop"
    priority = 60  # 介于 MissionGate(50) 与 DoomLoop(70) 之间

    def __init__(
        self,
        loop_dir: Optional[str] = None,
        done_file: str = "DONE",
        status_file: str = "status.json",
        max_turns: int = 100,
    ):
        self.loop_dir = loop_dir
        self.done_file = done_file
        self.status_file = status_file
        self.max_turns = max_turns

    def _is_complete(self) -> tuple[bool, str]:
        if not self.loop_dir:
            return False, ""
        done_path = os.path.join(self.loop_dir, self.done_file)
        if os.path.exists(done_path):
            return True, f"done sentinel found: {done_path}"
        status_path = os.path.join(self.loop_dir, self.status_file)
        if os.path.exists(status_path):
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("done") is True or data.get("status") == "complete":
                    return True, f"status file marks complete: {status_path}"
            except Exception:
                pass
        return False, ""

    def _remaining_hint(self) -> str:
        if not self.loop_dir:
            return ""
        status_path = os.path.join(self.loop_dir, self.status_file)
        if os.path.exists(status_path):
            try:
                with open(status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                remaining = data.get("remaining") or data.get("todo") or []
                if remaining:
                    return "剩余任务：" + "; ".join(str(x) for x in remaining[:10])
            except Exception:
                pass
        return ""

    def check(self, turn: int, state: dict, gate_state: dict) -> StopHandlerResult | None:
        if turn >= self.max_turns:
            return StopHandlerResult(
                StopAction.STOP, reason=f"file loop timeout (max_turns={self.max_turns})"
            )
        complete, why = self._is_complete()
        if complete:
            return StopHandlerResult(StopAction.STOP, reason=f"file loop complete: {why}")
        hint = self._remaining_hint()
        if hint:
            return StopHandlerResult(
                StopAction.CONTINUE,
                continuation_message=(
                    "任务尚未完成（依据状态文件）。" + hint + "。请继续推进未完成项。"
                ),
                reason="file loop: task not complete, continue",
            )
        return None
