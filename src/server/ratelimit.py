"""固定窗口限流（内存），应用于 run/stream 端点，按客户端 IP 限流。

配置：config.rate_limit（每分钟上限，0 = 关闭）。
说明：单进程内存实现，足够本地/单实例；多实例部署应换 Redis 等共享存储。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import Request, HTTPException
from .config import get_config

_state: dict[str, list[float]] = defaultdict(list)
_lock = threading.Lock()


async def rate_limit(request: Request) -> None:
    cfg = get_config()
    # QwenPaw 对齐：优先使用 rate_limiter 分组，未启用则回退顶层 rate_limit（0=关闭）
    rl = cfg.rate_limiter if isinstance(cfg.rate_limiter, dict) else {}
    if rl.get("enabled"):
        limit = int(rl.get("requests_per_minute", 60))
    else:
        limit = cfg.rate_limit
    if not limit or limit <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60.0
    with _lock:
        bucket = _state[ip]
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= limit:
            retry = int(window - (now - bucket[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded, retry after {retry}s",
            )
        bucket.append(now)
