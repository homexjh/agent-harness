"""传输层安全 + 热配置：配置脱敏、鉴权开/关、限流。

用 FastAPI TestClient（独立进程，不依赖正在运行的服务）。
配置落盘到临时目录，避免触碰真实 config/。
"""
import os
import tempfile

# 必须在 import app 之前设定配置目录（config 模块在导入时读取该 env）
_TMP = tempfile.mkdtemp(prefix="agent_cfg_test_")
os.environ["AGENT_CONFIG_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402
from src.server.app import app  # noqa: E402

client = TestClient(app)


def _cleanup_config():
    import src.server.config as c

    c._instance = None
    if os.path.exists(c.CONFIG_PATH):
        os.remove(c.CONFIG_PATH)


def test_get_config_default_masked():
    _cleanup_config()
    r = client.get("/config")
    assert r.status_code == 200
    d = r.json()
    assert d["security"]["enable_auth"] is False
    assert d["llm"]["api_key"] in ("", None)


def test_post_config_saves_and_masks():
    _cleanup_config()
    r = client.post("/config", json={"llm": {"api_key": "sk-secret-123"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 回显脱敏
    g = client.get("/config").json()
    assert g["llm"]["api_key"] == "****"


def test_auth_toggle_enforces_token():
    _cleanup_config()
    # 开启鉴权 + 设置服务令牌（此刻鉴权尚关，保存开放）
    r = client.post(
        "/config",
        json={"security": {"enable_auth": True}, "service": {"api_key": "tok-xyz"}},
    )
    assert r.status_code == 200

    # 无令牌访问受保护端点 -> 401
    assert client.get("/config").status_code == 401
    # 错令牌 -> 401
    assert client.get("/config", headers={"x-api-key": "wrong"}).status_code == 401
    # 正确令牌 -> 200
    assert client.get("/config", headers={"x-api-key": "tok-xyz"}).status_code == 200
    # Authorization: Bearer 也认
    assert (
        client.get("/config", headers={"Authorization": "Bearer tok-xyz"}).status_code
        == 200
    )

    # 关闭鉴权，恢复
    client.post("/config", json={"security": {"enable_auth": False}})


def test_rate_limit_triggers():
    import src.server.ratelimit as rl

    _cleanup_config()
    # 开启鉴权并设令牌，便于对受保护的 /config 端点压测限流
    client.post(
        "/config",
        json={"security": {"enable_auth": True}, "service": {"api_key": "tok-rl"}},
    )
    # 把限流调到 1/分钟
    r0 = client.post(
        "/config", json={"rate_limit": 1}, headers={"x-api-key": "tok-rl"}
    )
    assert r0.status_code == 200

    # 清空限流桶，避免 setup 请求占用名额，确保测量干净
    rl._state.clear()

    h = {"x-api-key": "tok-rl"}
    first = client.post("/config", json={"llm": {}}, headers=h)
    assert first.status_code == 200
    second = client.post("/config", json={"llm": {}}, headers=h)
    assert second.status_code == 429

    # 复位：关闭鉴权 + 取消限流
    client.post(
        "/config",
        json={"security": {"enable_auth": False}, "rate_limit": 0},
        headers=h,
    )
    _cleanup_config()
