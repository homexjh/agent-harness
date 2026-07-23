"""意图分类器（闲聊默认 + 任务自动升级）测试。"""
from __future__ import annotations

import importlib

from src.harness import intent
from src.harness.intent import classify_intent


def test_empty_is_chat():
    assert classify_intent("") == "chat"
    assert classify_intent("   ") == "chat"
    assert classify_intent(None) == "chat"  # type: ignore[arg-type]


def test_pure_chat_stays_chat():
    for msg in [
        "你好",
        "今天天气怎么样",
        "谢谢你",
        "讲个笑话听听",
        "你觉得人工智能会取代程序员吗",
        "我今天写了代码",  # 陈述句，非请求 -> 不升
        "帮我看下今天的新闻",  # 新闻非重工具任务 -> chat
    ]:
        assert classify_intent(msg) == "chat", f"误判为任务: {msg!r}"


def test_coding_intent_escalates():
    for msg in [
        "帮我写个脚本",
        "写个爬虫抓取网页",
        "运行这个命令",
        "帮我部署到 docker",
        "修复这个 bug",
        "改一下这个文件",
        "初始化一个项目",
        "帮我看看这个项目",
        "git 提交一下改动",
        "调试一下这个报错",
    ]:
        assert classify_intent(msg) == "coding", f"漏判为闲聊: {msg!r}"


def test_negation_stays_chat():
    assert classify_intent("不要写代码") == "chat"
    assert classify_intent("别帮我改文件了") == "chat"


def test_escalation_can_be_disabled(monkeypatch):
    monkeypatch.setenv("INTENT_ESCALATION", "0")
    importlib.reload(intent)
    assert intent.classify_intent("帮我写个脚本") == "chat"
    monkeypatch.setenv("INTENT_ESCALATION", "1")
    importlib.reload(intent)
    assert intent.classify_intent("帮我写个脚本") == "coding"
