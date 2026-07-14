"""轻量可观测：结构化日志 + 计数器（轮次/门触发/折叠/召回/token/错误）。"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("harness")


def log_event(name: str, **fields):
    """统一的结构化事件日志（单行 JSON，便于采集）。"""
    rec = {"ts": round(time.time(), 3), "event": name, **fields}
    logger.info(json.dumps(rec, ensure_ascii=False, default=str))


class Metrics:
    """进程内计数器，供测试/可观测读取。"""

    def __init__(self):
        self.turns = 0
        self.gate_triggers = {}
        self.folds = 0
        self.folded_tokens = 0
        self.recalls = 0
        self.tool_calls = 0
        self.tool_denials = 0
        self.tool_pruned = 0  # 超大工具输出被截断的次数
        self.media_stripped = 0  # 从上下文剥离的媒体块数
        self.errors = 0
        self.tokens_sent = 0
        self.window_sizes = []
        # 时间指标（毫秒）：最近一次模型调用
        self.last_ttft_ms = 0.0  # 用户感知的首 token（首个非空答案内容）
        self.last_first_byte_ms = 0.0  # 首字节/首个 chunk（含空包）
        self.last_first_reasoning_ms = 0.0  # 首个思考内容
        self.last_first_answer_ms = 0.0  # 首个最终答案内容
        self.last_generation_ms = 0.0  # 生成总耗时

    def turn(self):
        self.turns += 1

    def reset(self):
        """每次新请求前重置计数器，保证 /metrics 展示的是当前请求状态。"""
        self.turns = 0
        self.gate_triggers = {}
        self.folds = 0
        self.folded_tokens = 0
        self.recalls = 0
        self.tool_calls = 0
        self.tool_denials = 0
        self.tool_pruned = 0
        self.media_stripped = 0
        self.errors = 0
        self.tokens_sent = 0
        self.window_sizes = []
        self.last_ttft_ms = 0.0
        self.last_first_byte_ms = 0.0
        self.last_first_reasoning_ms = 0.0
        self.last_first_answer_ms = 0.0
        self.last_generation_ms = 0.0

    def gate(self, name: str):
        self.gate_triggers[name] = self.gate_triggers.get(name, 0) + 1
        log_event("gate_trigger", gate=name)

    def fold(self, n_msgs: int, tokens: int):
        self.folds += 1
        self.folded_tokens += tokens
        log_event("context_fold", msgs=n_msgs, tokens=tokens)

    def recall(self, n: int):
        self.recalls += 1
        log_event("recall", restored=n)

    def tool_call(self, name: str, allowed: bool):
        self.tool_calls += 1
        if not allowed:
            self.tool_denials += 1
        log_event("tool_call", tool=name, allowed=allowed)

    def prune(self, tool: str, original: int, kept: int):
        self.tool_pruned += 1
        log_event("tool_result_pruned", tool=tool, original=original, kept=kept)

    def media_strip(self, n: int):
        self.media_stripped += n
        log_event("media_stripped", count=n)

    def error(self, where: str, kind: str):
        self.errors += 1
        log_event("error", where=where, kind=kind)

    def context_window(self, size: int, tokens: int, folded: int):
        self.window_sizes.append(size)
        # 累积统计不再 reset，限制列表长度避免内存无限增长（仅保留最近 64 个窗口）。
        if len(self.window_sizes) > 64:
            self.window_sizes = self.window_sizes[-64:]
        self.tokens_sent = tokens
        log_event("context_window", size=size, tokens=tokens, folded=folded)

    def snapshot(self) -> dict:
        return {
            "turns": self.turns,
            "gate_triggers": self.gate_triggers,
            "folds": self.folds,
            "folded_tokens": self.folded_tokens,
            "recalls": self.recalls,
            "tool_calls": self.tool_calls,
            "tool_denials": self.tool_denials,
            "tool_pruned": self.tool_pruned,
            "media_stripped": self.media_stripped,
            "errors": self.errors,
            "tokens_sent": self.tokens_sent,
            "window_sizes": self.window_sizes,
            "ttft_ms": self.last_ttft_ms,
            "first_byte_ms": self.last_first_byte_ms,
            "first_reasoning_ms": self.last_first_reasoning_ms,
            "first_answer_ms": self.last_first_answer_ms,
            "generation_ms": self.last_generation_ms,
        }
