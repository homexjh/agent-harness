"""审批 HITL 看门狗：对齐 QwenPaw 的 ApprovalService 超时机制。

QwenPaw 的 ``PendingApproval`` 带 ``timeout_seconds``（默认 300s），并有 GC 清理超龄挂起，
因此未决审批**自动过期**，永远不会把某个会话永久卡在 HITL 中断上。

本模块在 LangGraph 的 interrupt 之上补同样的语义：
- 每次检测到本线程有未决 interrupt，登记 ``{thread_id: created_at}``；
- 后台 reaper 每 ~10s 扫描，超过 ``_approval_timeout_seconds()``（默认对齐 QwenPaw 300s，可由 /config 覆盖）的未决中断，
  调用注入的 ``on_expire(thread_id)`` 回调（由 app.py 实现真正的拒绝恢复）；
- 用户正常裁决（resume）或线程重置时清除登记项。

这样即使前端没弹审批、用户没点，thread 也会在超时后自动解除卡死。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 默认对齐 QwenPaw：300s；运行时可通过 /config 的 running.approval_timeout_seconds 覆盖
def _approval_timeout_seconds() -> float:
    try:
        from .config import get_config

        cfg = get_config()
        running = cfg.running if isinstance(cfg.running, dict) else {}
        val = float(running.get("approval_timeout_seconds", 300))
        return max(10.0, val)
    except Exception:
        return 300.0


_REAPER_INTERVAL_SECONDS = 10.0

# thread_id -> 登记时间戳（unix）
_pending: dict[str, float] = {}
_pending_lock = threading.Lock()

# 由 app.py 注入：thread_id -> 以「拒绝」裁决恢复图（超时自动否决）
_on_expire: Optional[Callable[[str], Awaitable[None]]] = None
# 可选回调：登记/清除时通知（前端可据此刷新审批卡片）
_on_change: Optional[Callable[[str, bool], None]] = None


def configure(
    *,
    on_expire: Callable[[str], Awaitable[None]],
    on_change: Optional[Callable[[str, bool], None]] = None,
) -> None:
    """注入超时恢复回调与变更通知（避免循环导入）。"""
    global _on_expire, _on_change
    _on_expire = on_expire
    _on_change = on_change


def register_pending(thread_id: str) -> None:
    """登记一个未决审批中断。"""
    with _pending_lock:
        _pending[thread_id] = time.time()
    logger.info("approval pending registered thread=%s (timeout=%.0fs)", thread_id, _approval_timeout_seconds())
    if _on_change:
        try:
            _on_change(thread_id, True)
        except Exception:  # noqa: BLE001
            pass


def clear_pending(thread_id: str) -> None:
    """清除某线程的未决审批登记（用户已裁决或线程已重置）。"""
    with _pending_lock:
        existed = _pending.pop(thread_id, None) is not None
    if existed and _on_change:
        try:
            _on_change(thread_id, False)
        except Exception:  # noqa: BLE001
            pass


def has_pending(thread_id: str) -> bool:
    with _pending_lock:
        return thread_id in _pending


def pending_count() -> int:
    with _pending_lock:
        return len(_pending)


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
        now = time.time()
        timeout = _approval_timeout_seconds()
        with _pending_lock:
            stale = [tid for tid, ts in list(_pending.items()) if now - ts > timeout]
        for tid in stale:
            with _pending_lock:
                _pending.pop(tid, None)
            logger.info("approval watchdog: thread=%s expired after %.0fs, auto-denying", tid, timeout)
            if _on_expire is not None:
                try:
                    await _on_expire(tid)
                except Exception:  # noqa: BLE001
                    logger.exception("approval watchdog on_expire error thread=%s", tid)
            if _on_change:
                try:
                    _on_change(tid, False)
                except Exception:  # noqa: BLE001
                    pass


_reaper_task: Optional[asyncio.Task] = None


def start_watchdog() -> None:
    """在事件循环内启动后台 reaper（应用启动时调用一次）。"""
    global _reaper_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        logger.warning("approval watchdog: no running loop, reaper not started")
        return
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = loop.create_task(_reaper_loop(), name="approval-watchdog")
        logger.info("approval watchdog reaper started (timeout=%.0fs)", _approval_timeout_seconds())
