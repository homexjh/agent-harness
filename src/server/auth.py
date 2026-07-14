# -*- coding: utf-8 -*-
"""服务级鉴权与多用户识别。

策略：按 config.security.enable_auth 开关。
- 关闭（默认）：单用户开箱即用，所有请求归属默认用户 ``"default"``，行为与改造前完全一致。
- 开启：请求必须携带由本模块签发的令牌（``Authorization: Bearer <token>`` 或
  ``x-api-key: <token>``），令牌解出 ``user_id``；无效则 401。
  令牌由 ``POST /auth/login`` 用 ``security.users`` 校验后签发（HMAC 签名 + 过期）。

注意：本模块只负责「识别用户身份」，隔离的落地（会话复合键、context/memory 按用户目录）
由各自存储层基于 user_ctx 完成。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import Request, HTTPException
from .config import get_config
from .user_ctx import SYSTEM_USER


# ---------------------------------------------------------------------------
# 令牌（HMAC 签名，非 JWT 依赖）
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    # 优先用 service.api_key 作签名密钥；开启鉴权时通常已配置。
    key = (get_config().service.get("api_key") or "").strip()
    if not key:
        # 兜底密钥（仅本地未设 api_key 时）；提示但不阻断。
        return b"agent-harness-local-secret"
    return key.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def encode_token(user_id: str, ttl: int = 60 * 60 * 24 * 30) -> str:
    """签发一个携带 user_id 的签名令牌（默认 30 天过期）。"""
    payload = {"sub": user_id, "exp": int(time.time()) + ttl}
    body = _b64(json.dumps(payload).encode("utf-8"))
    sig = _b64(hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: Optional[str]) -> Optional[str]:
    """校验签名与过期，返回 user_id；失败返回 None。"""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        exp_sig = _b64(hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, exp_sig):
            return None
        payload = json.loads(_b64d(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 登录校验
# ---------------------------------------------------------------------------
def verify_login(username: str, password: str) -> Optional[str]:
    """校验用户名/密码（明文，本地自托管场景）。成功返回 role，否则 None。"""
    users = (get_config().security.get("users") or [])
    for u in users:
        if u.get("username") == username and u.get("password") == password:
            return u.get("role", "user")
    return None


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------
def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or None


async def require_auth(request: Request) -> str:
    """返回当前请求的用户标识（user_id）。

    - 未开启鉴权 → ``"default"``（单用户，向后兼容）。
    - 开启鉴权 → 解析令牌，无效则 401。
    """
    cfg = get_config()
    if not cfg.security.get("enable_auth"):
        return "default"
    uid = decode_token(_extract_token(request))
    if not uid:
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return uid
