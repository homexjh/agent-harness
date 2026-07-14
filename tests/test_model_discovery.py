"""测试模型厂商目录 + 发现/校验逻辑（含 /providers、/models 端点）。"""
import os
import sys
import tempfile

# 必须在 import app 之前设定配置目录（config 模块在导入时读取该 env）
_TMP = tempfile.mkdtemp(prefix="agent_cfg_test_")
os.environ["AGENT_CONFIG_DIR"] = _TMP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from src.server import model_discovery as md  # noqa: E402
from src.server.app import app  # noqa: E402

client = TestClient(app)


def test_providers_registry():
    provs = md.list_providers()
    ids = {p["id"] for p in provs}
    assert {"deepseek", "openai", "qwen", "custom"}.issubset(ids)
    # deepseek / qwen 应标为支持 reasoning
    by_id = {p["id"]: p for p in provs}
    assert by_id["deepseek"]["reasoning"] is True
    assert by_id["qwen"]["reasoning"] is True
    assert by_id["moonshot"]["reasoning"] is False
    # 每个厂商有默认 base_url（custom 允许为空）
    for p in provs:
        if p["id"] != "custom":
            assert p["base_url"].startswith("http")


def test_normalize_base_url():
    assert md.normalize_base_url("  https://api.deepseek.com/v1/  ") == "https://api.deepseek.com/v1"
    assert md.normalize_base_url("https://api.deepseek.com/v1/chat/completions") == "https://api.deepseek.com/v1"


def test_match_provider():
    assert md.match_provider("https://api.deepseek.com/v1") == "deepseek"
    assert md.match_provider("https://dashscope.aliyuncs.com/compatible-mode/v1") == "qwen"
    assert md.match_provider("https://example.com/foo") is None


def test_supports_reasoning():
    assert md._supports_reasoning("deepseek", ["deepseek-reasoner"]) is True
    assert md._supports_reasoning(None, ["qwen3-235b-a22b"]) is True
    assert md._supports_reasoning(None, ["llama3"]) is False


def test_discover_missing_key_returns_400_like():
    # discover_models 需要 api_key；这里直接验证 _discover_sync 对空 base_url 的兜底
    res = md._discover_sync("", "sk-x")
    assert res["ok"] is False
    assert res["key_valid"] is False


def test_http_endpoints():
    """用 FastAPI TestClient 验证 /providers 与 /models（缺 key 应 400）。"""
    r = client.get("/providers")
    assert r.status_code == 200
    data = r.json()
    assert any(p["id"] == "deepseek" for p in data["providers"])

    # 缺 api_key -> 400
    r2 = client.post("/models", json={"base_url": "https://api.deepseek.com/v1"})
    assert r2.status_code == 400

    # 无效 key + 不可达 host -> 返回 ok=False 或 key_valid=False（不崩溃）
    r3 = client.post(
        "/models",
        json={"base_url": "https://api.deepseek.com/v1", "api_key": "sk-invalid-test"},
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body["key_valid"] is False
