"""插件管理端点（Files/Tools/Skills/MCP/ACP/Agents/Cron/Heartbeat/Sessions）基础集成测试。"""
from fastapi.testclient import TestClient

from src.server.app import app

client = TestClient(app)


def test_workspace_crud():
    # 创建
    r = client.post("/workspace", json={"path": "test/hello.txt", "content": "world"})
    assert r.status_code == 200, r.text

    # 读取目录
    r = client.get("/workspace")
    assert r.status_code == 200
    entries = {e["name"] for e in r.json()["entries"]}
    assert "test" in entries

    # 读取文件
    r = client.get("/workspace/read?path=test/hello.txt")
    assert r.status_code == 200
    assert r.json()["content"] == "world"

    # 重命名
    r = client.patch("/workspace", json={"old": "test/hello.txt", "new": "test/bye.txt"})
    assert r.status_code == 200

    # 删除
    r = client.delete("/workspace?path=test")
    assert r.status_code == 200


def test_tools_list():
    r = client.get("/tools")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()}
    assert "write_file" in names
    assert "exec" in names


def test_agents_default():
    r = client.get("/agents")
    assert r.status_code == 200
    assert any(a["id"] == "default" for a in r.json()["agents"])


def test_acp_list():
    r = client.get("/acp")
    assert r.status_code == 200
    agents = r.json().get("agents", {})
    assert "opencode" in agents


def test_heartbeat():
    r = client.get("/heartbeat")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "memory" in r.json()


def test_mcp_crud():
    r = client.post("/mcp", json={"key": "test", "name": "Test MCP", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]})
    assert r.status_code == 200
    r = client.get("/mcp")
    keys = {c["key"] for c in r.json().get("clients", [])}
    assert "test" in keys
    r = client.delete("/mcp/test")
    assert r.status_code == 200


def test_cron_crud():
    r = client.post("/cron", json={"name": "demo", "schedule": "0 9 * * *", "command": "say good morning"})
    assert r.status_code == 200
    r = client.get("/cron")
    assert any(j["name"] == "demo" for j in r.json()["jobs"])
    jid = [j["id"] for j in r.json()["jobs"] if j["name"] == "demo"][0]
    r = client.delete(f"/cron/{jid}")
    assert r.status_code == 200
