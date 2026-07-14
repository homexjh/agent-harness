"""模型厂商目录 + 模型发现（OpenAI 兼容 /models 探测 + 已知模型回退）。

设计目标（对齐 QwenPaw 的交互）：用户直接「选厂商」即可带出默认 base_url 与常用模型，
点「发现模型」时后端拿 key 去探测该厂商的可用模型并校验密钥是否有效——
从而让用户「知道自己配没配成功、能选哪些模型」。

所有厂商都走 OpenAI 兼容协议（/v1/chat/completions、/v1/models），
因此一套发现逻辑即可覆盖 DeepSeek / OpenAI / Qwen / 智谱 / Kimi / 硅基流动 / Ollama 等。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

# 专属线程池：模型探测是同步 urllib 网络调用（最长 ~24s），
# 必须隔离在默认线程池之外，否则会抢占聊天路径（get_request_graph / LangGraph 同步节点
# 也用默认线程池），导致偶发数十秒卡顿（"卡住很久"症状的一类根因）。
_discover_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="model-discover")


# ---------------------------------------------------------------------------
# 厂商目录：id -> 元信息。新增厂商只需在此追加一条。
# ---------------------------------------------------------------------------
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "reasoning": True,
        "models": ["deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner"],
        "doc": "https://platform.deepseek.com",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "reasoning": True,
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3-mini"],
        "doc": "https://platform.openai.com",
    },
    "qwen": {
        "label": "阿里云百炼 / Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "reasoning": True,
        "models": [
            "qwen-max",
            "qwen-plus",
            "qwen-turbo",
            "qwen2.5-72b-instruct",
            "qwen3-235b-a22b",
        ],
        "doc": "https://help.aliyun.com/zh/model-studio",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "reasoning": True,
        "models": ["glm-4-plus", "glm-4-air", "glm-z1-air"],
        "doc": "https://open.bigmodel.cn",
    },
    "moonshot": {
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "reasoning": False,
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "doc": "https://platform.moonshot.cn",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "reasoning": False,
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-72B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
        ],
        "doc": "https://siliconflow.cn",
    },
    "ollama": {
        "label": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "reasoning": False,
        "models": ["llama3", "qwen2.5", "deepseek-r1"],
        "doc": "http://localhost:11434",
    },
    "custom": {
        "label": "自定义 / 其他 OpenAI 兼容",
        "base_url": "",
        "reasoning": False,
        "models": [],
        "doc": "",
    },
}


def normalize_base_url(base_url: str) -> str:
    """规整用户输入的 base_url：去空白、去尾部 /、去掉误填的 /chat/completions。"""
    base = (base_url or "").strip().rstrip("/")
    base = re.sub(r"/chat/completions/?$", "", base)
    return base


def match_provider(base_url: str) -> Optional[str]:
    """按 host 匹配已知厂商（用于回退已知模型 + 识别 reasoning 支持）。"""
    b = (base_url or "").lower()
    for pid, p in PROVIDERS.items():
        if pid == "custom":
            continue
        if p["base_url"] and p["base_url"].lower() in b:
            return pid
    return None


def _http_json(url: str, api_key: str, method: str = "GET", body: Optional[bytes] = None, timeout: float = 12.0):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(raw) if raw else {})


def _reasoning_models(pid: Optional[str], models: List[str]) -> List[str]:
    """从模型列表中挑出明确支持 reasoning 的模型。"""
    if pid and pid in PROVIDERS:
        known = PROVIDERS[pid].get("models", [])
        # 按厂商精确匹配
        if pid == "deepseek":
            return [m for m in models if "reasoner" in m.lower()]
        if pid == "openai":
            return [m for m in models if any(k in m.lower() for k in ("o1", "o3-"))]
        if pid == "qwen":
            return [m for m in models if any(k in m.lower() for k in ("qwen3", "qwq"))]
        if pid == "zhipu":
            return [m for m in models if "z1" in m.lower()]
    # 兜底：按模型名关键词
    return [
        m
        for m in models
        if any(k in m.lower() for k in ("reason", "r1", "z1", "o1", "o3", "thinking", "qwen3", "qwq"))
    ]


def _supports_reasoning(pid: Optional[str], models: List[str]) -> bool:
    if pid and PROVIDERS.get(pid, {}).get("reasoning"):
        return bool(_reasoning_models(pid, models))
    return bool(_reasoning_models(pid, models))


def _discover_sync(base_url: str, api_key: str) -> Dict[str, Any]:
    """同步发现逻辑（在 to_thread 中执行，避免阻塞事件循环）。"""
    base = normalize_base_url(base_url)
    if not base:
        return {
            "ok": False,
            "error": "缺少 base_url",
            "provider": None,
            "key_valid": False,
            "discovered": False,
            "models": [],
        }

    pid = match_provider(base)
    known = PROVIDERS.get(pid, {}).get("models", []) if pid else []

    models_list: List[str] = []
    key_valid = False
    error: Optional[str] = None

    # 1) 优先走 OpenAI 兼容的 /models 列举
    try:
        status, data = _http_json(f"{base}/models", api_key)
        if status == 200:
            key_valid = True
            for m in data.get("data", []):
                mid = m.get("id") or m.get("name")
                if mid and mid not in models_list:
                    models_list.append(mid)
        elif status in (401, 403):
            key_valid = False
            error = f"鉴权失败（HTTP {status}）：API Key 无效或无权访问"
        else:
            error = f"列举模型返回 HTTP {status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            key_valid = False
            error = f"鉴权失败（HTTP {e.code}）：API Key 无效或无权访问"
        else:
            error = f"列举模型失败：HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        error = f"无法连接：{e}"

    discovered = bool(models_list)

    # 2) 厂商不支持 /models 列举时：回退到已知模型，并做一次最小 chat 完成来校验 key
    if not discovered:
        models_list = list(known)
        if not key_valid and known:
            try:
                test_body = json.dumps(
                    {
                        "model": known[0],
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    }
                ).encode()
                st, _ = _http_json(f"{base}/chat/completions", api_key, "POST", test_body)
                if st == 200:
                    key_valid = True
                    error = None
                elif st in (401, 403):
                    key_valid = False
                    error = f"鉴权失败（HTTP {st}）"
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    key_valid = False
                    error = f"鉴权失败（HTTP {e.code}）"
            except Exception:
                pass

    return {
        "ok": True,
        "provider": pid,
        "provider_label": PROVIDERS.get(pid, {}).get("label"),
        "key_valid": key_valid,
        "discovered": discovered,
        "supports_reasoning": _supports_reasoning(pid, models_list),
        "reasoning_models": _reasoning_models(pid, models_list),
        "models": models_list,
        "error": error,
    }


async def discover_models(base_url: str, api_key: str) -> Dict[str, Any]:
    """异步包装：把阻塞的网络 IO 丢到**专属**线程池，保护 FastAPI 事件循环，

    且不抢占聊天路径（get_request_graph / LangGraph 同步节点）使用的默认线程池。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_discover_executor, _discover_sync, base_url, api_key)


def list_providers() -> List[Dict[str, Any]]:
    return [
        {
            "id": pid,
            "label": p["label"],
            "base_url": p["base_url"],
            "reasoning": p["reasoning"],
            "models": p["models"],
            "doc": p.get("doc", ""),
        }
        for pid, p in PROVIDERS.items()
    ]
