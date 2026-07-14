"""重试 + 指数退避 + 熔断（模型/工具调用的稳定性底座）。"""
from __future__ import annotations

import random
import time
from functools import wraps


class TransientError(Exception):
    """可被重试的瞬时错误（网络超时、限流、5xx）。"""


def with_retry(
    fn,
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    backoff: float = 2.0,
    jitter: float = 0.3,
):
    """对 fn() 做指数退避重试，仅对 TransientError 重试。"""
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except TransientError as e:
            last = e
            if attempt == max_attempts:
                break
            sleep = min(max_delay, base_delay * (backoff ** (attempt - 1)))
            sleep *= 1 + random.uniform(-jitter, jitter)
            time.sleep(max(0.0, sleep))
    raise last


class CircuitBreaker:
    """简单熔断：连续失败超阈值则开路，冷却后半开试探。"""

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"

    def _now(self) -> float:
        return time.time()

    def allow(self) -> bool:
        if self._state == "open":
            if self._now() - self._opened_at >= self.reset_timeout:
                self._state = "half-open"
                self._failures = 0
                return True
            return False
        return True

    def success(self):
        self._failures = 0
        self._state = "closed"

    def failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = self._now()

    @property
    def state(self) -> str:
        return self._state

    def call(self, fn):
        if not self.allow():
            raise TransientError(f"circuit breaker open (cooling down)")
        try:
            result = fn()
            self.success()
            return result
        except TransientError:
            self.failure()
            raise
