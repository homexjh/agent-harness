"""服务级鉴权（传输层安全之一）。

策略：按 config.security.enable_auth 开关。
- 关闭：本地开发放行（不校验），方便开箱即用。
- 开启：要求请求携带有效令牌，且与 config.service.api_key 一致。

支持的请求头（二者皆可）：
- Authorization: Bearer <token>
- x-api-key: <token>        # 与 @langchain/langgraph-sdk 的 Client({apiKey}) 默认行为一致
"""
from __future__ import annotations

from fastapi import Request, HTTPException
from .config import get_config


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or None


async def require_auth(request: Request) -> str | None:
    """FastAPI 依赖：开启鉴权时校验令牌，否则放行。"""
    cfg = get_config()
    if not cfg.security.get("enable_auth"):
        return None
    expected = cfg.service.get("api_key")
    if not expected:
        # 开了鉴权却没设令牌：拒绝，避免“开了等于没开”
        raise HTTPException(
            status_code=401,
            detail="service auth enabled but no token configured",
        )
    token = _extract_token(request)
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return token
