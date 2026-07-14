"""ApprovalHub 与审批安全策略回归测试。

覆盖本轮修复的两个关键 bug：
1. approval_hub.wait() 不吞 CancelledError（取消可正确传播）。
2. _is_approved 对未知/脏 decision 保守拒绝。
3. /api/approval 端点对 decision 做白名单校验。
"""
import asyncio
import sys
import uuid

from fastapi.testclient import TestClient

from src.harness.graph import _is_approved
from src.server.approval_hub import ApprovalHub
from src.server.app import app

client = TestClient(app)


def test_is_approved_known_values():
    assert _is_approved("approved") is True
    assert _is_approved("rejected") is False
    assert _is_approved("timeout") is False
    assert _is_approved(True) is True
    assert _is_approved(False) is False
    assert _is_approved(None) is True  # 旧客户端兼容


def test_is_approved_unknown_values_are_denied():
    """未知/拼写错误的 decision 必须按拒绝处理，防止越权放行。"""
    assert _is_approved("rejct") is False
    assert _is_approved("approve") is True  # 这是已知批准词，保留
    assert _is_approved("whatever") is False
    assert _is_approved("") is False
    assert _is_approved({"decision": "rejct"}) is False


def test_wait_returns_decision_on_resolve():
    async def main():
        hub = ApprovalHub()
        pa = hub.request("t", {"question": "ok?"}, 60)

        async def resolve_later():
            await asyncio.sleep(0.01)
            hub.resolve(pa.request_id, "approved")

        return await asyncio.gather(hub.wait(pa), resolve_later())

    result, _ = asyncio.run(main())
    assert result == "approved"


def test_wait_returns_rejected_on_timeout():
    async def main():
        hub = ApprovalHub()
        pa = hub.request("t", {"question": "ok?"}, 0.05)
        return await hub.wait(pa)

    result = asyncio.run(main())
    assert result == "rejected"


def test_wait_propagates_cancelled_error():
    """取消操作不应被吞掉；否则 graph_task.cancel() 无法真正停止图执行。"""
    async def main():
        hub = ApprovalHub()
        pa = hub.request("t", {"question": "ok?"}, 60)

        async def waiter():
            return await hub.wait(pa)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)  # 让 task 进入 wait_for
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "not-cancelled"

    result = asyncio.run(main())
    assert result == "cancelled"


def test_api_approval_rejects_unknown_decision():
    """端点对非法 decision 返回 400，而不是放行到审批逻辑。"""
    # 使用一个不存在的 request_id，确保即使绕过校验也不会误操作真实审批
    rid = f"nonexistent:{uuid.uuid4().hex[:12]}"
    r = client.post(
        "/api/approval/test-thread",
        json={"request_id": rid, "decision": "rejct"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_approval_accepts_rejected():
    """合法 decision 即使 request_id 不存在/过期，也应返回 404 而非 400。"""
    rid = f"nonexistent:{uuid.uuid4().hex[:12]}"
    r = client.post(
        "/api/approval/test-thread",
        json={"request_id": rid, "decision": "rejected"},
    )
    assert r.status_code == 404
