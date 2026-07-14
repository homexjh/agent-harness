# -*- coding: utf-8 -*-
"""多用户隔离集成测试（会话 / 上下文 / 记忆 / 鉴权）。

用 FastAPI TestClient（独立进程，不依赖正在运行的服务）。鉴权配置目录与运行时数据目录
都指向同一临时目录（AGENT_CONFIG_DIR + AGENT_DATA_HOME），每个测试前清空，零污染真实数据。

覆盖：
- 令牌签发 / 校验 / 防篡改 / 过期 / 畸形输入（auth 单测）
- 关闭鉴权时单用户默认行为（向后兼容，零行为变化）
- 开启鉴权后：登录签发令牌、无令牌 401、不同用户会话列表隔离
- 上下文配置按用户隔离
- 记忆保险库按用户目录隔离 + 自动记忆不跨用户
- 复合键 ``user:thread`` 不串台
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# 必须在 import app 之前设定配置目录与数据目录（config 模块在导入时读取这些 env）
_TMP = tempfile.mkdtemp(prefix="agent_iso_test_")
os.environ["AGENT_CONFIG_DIR"] = _TMP
os.environ["AGENT_DATA_HOME"] = _TMP  # 隔离所有运行时数据（sessions/context/memory/checkpoints）

from fastapi.testclient import TestClient  # noqa: E402

from src.server import auth as auth_mod  # noqa: E402
from src.server import config as cfg_mod  # noqa: E402
from src.server import memory as memory_mod  # noqa: E402
from src.server import graph_provider as gp_mod  # noqa: E402
from src.server.app import app, _ckpt_thread  # noqa: E402
from src.server.auth import encode_token, decode_token, verify_login  # noqa: E402
from src.server.user_ctx import get_user, set_user, reset_user  # noqa: E402

client = TestClient(app)

USERS = [
    {"username": "alice", "password": "pwA", "role": "user"},
    {"username": "bob", "password": "pwB", "role": "user"},
]


@pytest.fixture(autouse=True)
def iso_setup():
    """每个测试前清空运行时数据（会话索引/记忆/checkpoint）与配置落盘，重置全局单例/缓存。

    注意：config.DATA_HOME 在模块导入期被绑定为进程级常量（受最先导入的测试模块影响），
    不一定等于本模块的 ``_TMP``。因此这里直接清除「真实」路径（DATA_HOME / CONFIG_PATH /
    sessions.json），而非本模块的 _TMP，才能确保互不污染、无残留会话串台。
    """
    from src.server.plugins import _db_path

    data_home = cfg_mod.DATA_HOME
    # 清空 DATA_HOME 下所有运行时数据
    if data_home.exists():
        for p in data_home.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    # 删除会话索引与配置落盘（避免向上/下游测试模块泄漏 enable_auth 等状态）
    sp = _db_path("sessions.json")
    if sp.exists():
        sp.unlink()
    cp = Path(cfg_mod.CONFIG_PATH)
    if cp.exists():
        cp.unlink()
    cfg_mod._instance = None
    memory_mod.reset_memory_manager()
    gp_mod.reset_context_manager()
    yield
    # teardown：复位，避免向其它测试模块泄漏 enable_auth=True
    cfg_mod._instance = None
    cp = Path(cfg_mod.CONFIG_PATH)
    if cp.exists():
        cp.unlink()
    memory_mod.reset_memory_manager()
    gp_mod.reset_context_manager()


# ---------------------------------------------------------------------------
# 鉴权：令牌与登录（单元层）
# ---------------------------------------------------------------------------
def test_token_roundtrip_and_tamper():
    t = encode_token("alice")
    assert decode_token(t) == "alice"
    # 篡改任意一个字符 -> 签名校验失败
    bad = t[:-1] + ("A" if t[-1] != "A" else "B")
    assert decode_token(bad) is None
    # 过期令牌
    assert decode_token(encode_token("x", ttl=-10)) is None
    # 畸形输入
    assert decode_token(None) is None
    assert decode_token("") is None
    assert decode_token("nodot") is None


def test_verify_login():
    cfg_mod._instance = cfg_mod.RuntimeConfig(
        security={"enable_auth": True, "users": USERS}
    )
    assert verify_login("alice", "pwA") == "user"
    assert verify_login("alice", "wrong") is None
    assert verify_login("ghost", "x") is None


# ---------------------------------------------------------------------------
# 用户上下文 ContextVar
# ---------------------------------------------------------------------------
def test_user_ctx_scoping():
    assert get_user() == "default"
    tok = set_user("alice")
    assert get_user() == "alice"
    reset_user(tok)
    assert get_user() == "default"


# ---------------------------------------------------------------------------
# 复合键
# ---------------------------------------------------------------------------
def test_composite_key_format():
    assert _ckpt_thread("alice", "t1") == "alice:t1"
    assert _ckpt_thread("bob", "t1") == "bob:t1"
    assert _ckpt_thread("default", "abc") == "default:abc"


# ---------------------------------------------------------------------------
# 关闭鉴权：单用户默认行为（向后兼容）
# ---------------------------------------------------------------------------
def test_default_single_user_no_auth():
    # 默认（未开启鉴权）：受保护端点直接 200，归属 default 用户
    assert client.get("/sessions").status_code == 200
    tid = client.post("/sessions").json()["thread_id"]
    sessions = client.get("/sessions").json()["sessions"]
    assert any(s["id"] == tid for s in sessions)


# ---------------------------------------------------------------------------
# 开启鉴权：登录签发 + 无令牌 401
# ---------------------------------------------------------------------------
def _enable_auth():
    # 直接写配置单例，避免通过 POST /config 开启鉴权后该端点自身也要求令牌（自锁）。
    cfg_mod.save_config(
        {
            "security": {"enable_auth": True, "users": USERS},
            "service": {"api_key": "test-secret"},
        }
    )


def _auth_headers(username: str, password: str) -> dict:
    _enable_auth()
    r = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_auth_status_reports_enabled():
    _enable_auth()
    assert client.get("/auth/status").json()["enable_auth"] is True


def test_login_success_and_failure():
    _enable_auth()
    ok = client.post("/auth/login", json={"username": "alice", "password": "pwA"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True and body["user_id"] == "alice"
    bad = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert bad.status_code == 401


def test_protected_requires_token_when_auth_on():
    _enable_auth()
    # 无令牌 -> 401
    assert client.get("/sessions").status_code == 401
    # 错令牌 -> 401
    assert (
        client.get("/sessions", headers={"Authorization": "Bearer garbage.token"}).status_code
        == 401
    )
    # 合法令牌 -> 200
    h = _auth_headers("alice", "pwA")
    assert client.get("/sessions", headers=h).status_code == 200


# ---------------------------------------------------------------------------
# 会话隔离：不同用户不可见彼此会话
# ---------------------------------------------------------------------------
def test_session_isolation_between_users():
    ha = _auth_headers("alice", "pwA")
    hb = _auth_headers("bob", "pwB")
    ta = client.post("/sessions", headers=ha).json()["thread_id"]
    tb = client.post("/sessions", headers=hb).json()["thread_id"]

    sa = client.get("/sessions", headers=ha).json()["sessions"]
    sb = client.get("/sessions", headers=hb).json()["sessions"]
    assert {s["id"] for s in sa} == {ta}
    assert {s["id"] for s in sb} == {tb}
    # 互不可见
    assert ta not in {s["id"] for s in sb}
    assert tb not in {s["id"] for s in sa}


def test_session_user_attribution_isolated():
    """不同用户用各自独立的 thread id 创建会话：列表按 user 过滤隔离，且索引落盘的 user_id 归属正确。

    注：会话索引以 tid 为键（真实场景 tid 由服务端生成 uuid4，不会跨用户碰撞）；
    跨用户隔离由索引中的 user_id 过滤 + checkpointer 的复合键 ``user:thread`` 双重保证。
    """
    ha = _auth_headers("alice", "pwA")
    hb = _auth_headers("bob", "pwB")
    ta = client.post("/sessions", json={"thread_id": "a-thread"}, headers=ha).json()["thread_id"]
    tb = client.post("/sessions", json={"thread_id": "b-thread"}, headers=hb).json()["thread_id"]

    sa = {s["id"] for s in client.get("/sessions", headers=ha).json()["sessions"]}
    sb = {s["id"] for s in client.get("/sessions", headers=hb).json()["sessions"]}
    assert sa == {ta} and sb == {tb}
    # 互不可见
    assert ta not in sb and tb not in sa

    # 直接校验持久化索引中的 user_id 归属
    import json as _json

    from src.server.plugins import _db_path

    idx = _json.loads(_db_path("sessions.json").read_text(encoding="utf-8"))
    assert idx["threads"][ta]["user_id"] == "alice"
    assert idx["threads"][tb]["user_id"] == "bob"


# ---------------------------------------------------------------------------
# 上下文配置按用户隔离
# ---------------------------------------------------------------------------
def test_context_config_isolation():
    ha = _auth_headers("alice", "pwA")
    hb = _auth_headers("bob", "pwB")

    # alice 改配置
    r = client.put(
        "/context/config", json={"config": {"budget_tokens": 12345}}, headers=ha
    )
    assert r.status_code == 200
    assert r.json()["budget_tokens"] == 12345

    # alice 读到自己的
    assert (
        client.get("/context/config", headers=ha).json()["budget_tokens"] == 12345
    )
    # bob 读到默认（8000），看不到 alice 的值
    assert client.get("/context/config", headers=hb).json()["budget_tokens"] == 8000


# ---------------------------------------------------------------------------
# 记忆保险库按用户目录隔离（不依赖 LLM：auto_memory 无 key 走启发式回退）
# ---------------------------------------------------------------------------
def test_memory_manager_isolation():
    memory_mod.reset_memory_manager()
    mm_a = memory_mod.get_memory_manager("alice")
    mm_b = memory_mod.get_memory_manager("bob")

    assert mm_a is not mm_b
    # vault 目录按用户隔离：DATA_HOME/{user}/memory_vault
    assert "alice" in str(mm_a.vault.daily_dir)
    assert "bob" in str(mm_b.vault.daily_dir)

    # alice 写一条自动记忆（无 LLM key 走启发式回退，不触发网络）
    mm_a.auto_memory(
        [{"type": "user", "content": "I am Alice and I like cats."}], force=True
    )
    assert mm_a.vault.stats().get("notes", 0) >= 1
    # bob 的 vault 没有 alice 的笔记
    assert mm_b.vault.stats().get("notes", 0) == 0


def test_memory_config_persists_per_user():
    """记忆配置落盘到 DATA_HOME/{user}/memory_config.json，按用户隔离。"""
    cfg_a = memory_mod.MemoryManagerConfig(
        reme_light_memory_config=memory_mod.ReMeLightMemoryConfig(auto_memory_interval=3)
    )
    cfg_b = memory_mod.MemoryManagerConfig(
        reme_light_memory_config=memory_mod.ReMeLightMemoryConfig(auto_memory_interval=7)
    )
    memory_mod.save_config(cfg_a, "alice")
    memory_mod.save_config(cfg_b, "bob")

    assert (
        memory_mod.load_config("alice").reme_light_memory_config.auto_memory_interval == 3
    )
    assert (
        memory_mod.load_config("bob").reme_light_memory_config.auto_memory_interval == 7
    )
