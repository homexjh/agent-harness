"""Token Usage 记录与统计 —— 对齐 QwenPaw /token-usage。

设计原则（对应「不要搞乱时延」）：
- 记录完全**非阻塞**：LangChain 回调在每次 LLM 调用结束后触发，把写盘动作
  丢到 daemon 线程里执行，绝不占用流式响应主路径。
- 读路径（前端轮询）只做聚合，不写盘。
- 文件上限 5000 条，超过后只保留最近，避免无限增长拖慢读写。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

from .config import DATA_HOME

_DB_PATH = DATA_HOME / "token_usage.json"
_MAX_RECORDS = 5000
_lock = threading.Lock()


def _read_all() -> List[Dict[str, Any]]:
    try:
        if _DB_PATH.exists():
            data = json.loads(_DB_PATH.read_text(encoding="utf-8")) or []
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def record(
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: Optional[float] = None,
    thread_id: Optional[str] = None,
) -> None:
    """追加一条用量记录（后台线程写盘，fire-and-forget）。"""

    def _write() -> None:
        try:
            rec = {
                "ts": time.time(),
                "date": time.strftime("%Y-%m-%d"),
                "hour": time.strftime("%Y-%m-%d %H:00"),
                "provider": provider or "unknown",
                "model": model or "unknown",
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "duration_ms": (int(duration_ms) if duration_ms is not None else None),
                "thread_id": thread_id,
            }
            with _lock:
                data = _read_all()
                data.append(rec)
                if len(data) > _MAX_RECORDS:
                    data = data[-_MAX_RECORDS:]
                _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _DB_PATH.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            # 用量统计失败绝不影响主流程
            pass

    t = threading.Thread(target=_write, daemon=True)
    t.start()


def read_details(limit: int = 200) -> List[Dict[str, Any]]:
    data = _read_all()
    return data[-limit:][::-1]  # 最新的在前


def read_summary() -> Dict[str, Any]:
    """聚合：总计、按日期、按模型、按厂商。"""
    data = _read_all()
    total_tokens = 0
    total_prompt = 0
    total_completion = 0
    by_date: Dict[str, int] = {}
    by_model: Dict[str, int] = {}
    by_provider: Dict[str, int] = {}
    for r in data:
        pt = int(r.get("prompt_tokens", 0) or 0)
        ct = int(r.get("completion_tokens", 0) or 0)
        tt = int(r.get("total_tokens", 0) or 0)
        total_prompt += pt
        total_completion += ct
        total_tokens += tt
        d = r.get("date", "unknown")
        by_date[d] = by_date.get(d, 0) + tt
        m = r.get("model", "unknown")
        by_model[m] = by_model.get(m, 0) + tt
        p = r.get("provider", "unknown")
        by_provider[p] = by_provider.get(p, 0) + tt
    # 日期排序
    by_date_sorted = dict(sorted(by_date.items()))
    top_models = dict(
        sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)[:10]
    )
    top_providers = dict(
        sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)[:10]
    )
    return {
        "count": len(data),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "by_date": by_date_sorted,
        "by_model": top_models,
        "by_provider": top_providers,
    }


class TokenUsageCallbackHandler(BaseCallbackHandler):
    """挂在模型上的回调：每次 LLM 调用结束抓取 token 用量并异步记录。

    不订阅流式 token 事件（on_llm_new_token），只在 on_llm_end 处理最终用量，
    因此对逐 token 流式输出零开销。
    """

    def __init__(self, provider: str = "", model: str = ""):
        super().__init__()
        self.provider = provider
        self.model = model

    def on_llm_end(self, response, *, run_id=None, parent_run_id=None, **kwargs):
        try:
            usage = None
            # 1) OpenAI 兼容：response.llm_output["token_usage"]
            llm_output = getattr(response, "llm_output", None)
            if isinstance(llm_output, dict):
                usage = llm_output.get("token_usage") or llm_output.get("usage")
            # 2) langchain usage_metadata（新版本 AIMessage）
            if not usage:
                um = getattr(response, "usage_metadata", None)
                if isinstance(um, dict):
                    usage = {
                        "prompt_tokens": um.get("input_tokens"),
                        "completion_tokens": um.get("output_tokens"),
                        "total_tokens": um.get("total_tokens"),
                    }
            if not usage:
                return
            dur = None
            start = getattr(response, "llm_start_time", None)
            if start:
                try:
                    dur = (time.time() - float(start)) * 1000
                except Exception:
                    dur = None
            record(
                provider=self.provider,
                model=self.model,
                prompt_tokens=usage.get("prompt_tokens") or 0,
                completion_tokens=usage.get("completion_tokens") or 0,
                total_tokens=usage.get("total_tokens") or 0,
                duration_ms=dur,
            )
        except Exception:
            pass
