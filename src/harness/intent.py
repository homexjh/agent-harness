"""意图识别：把用户消息粗分为 chat / coding，用于「闲聊默认 + 任务自动升级」。

这是 Option C（混合：chat 默认 + 意图升级）的核心判别器。后端不再盲信 mode，
而是在 chat 默认下对用户消息做一次**廉价启发式**分类；一旦判定为需要
文件 / 命令 / 编码类重工具的任务，就把会话从 chat 升到 coding（全工具），
并通知前端同步选择器。

设计原则：
- 保守：宁可漏判（留在 chat）也不误判（把闲聊升成 coding）。chat 下轻工具
  （读 / 时间 / 搜索 / 记忆 / 召回 / 截图）仍可完成多数事；误升只会让模型更
  “重”、更易误改环境。升级是可逆的——前端把 mode 锁定为 coding，用户随时可切回。
- 可关：env INTENT_ESCALATION=0 完全关闭，退化成纯手动（Option A）。

判定逻辑（两层）：
1. 强对象：出现即高度疑似任务（如“写个脚本”“运行命令”“修复 bug”），直接升。
2. 弱对象 + 请求标记：如“帮我看看这个项目”，升；但“我今天写了代码”这种陈述
   （仅有动作动词、无请求标记、且强对象不匹配）不升，避免误判。
"""

from __future__ import annotations

import os
import re

# 请求类标记：出现才允许把“代码 / 任务”词当作真实意图，抑制“我今天写了代码”这类陈述。
_REQUEST_MARKERS = [
    "帮我", "请", "麻烦", "能不能", "可以吗", "可否", "我想", "替我", "给我",
    "需要", "能否", "打算让你", "你帮我", "求", "帮我看", "帮忙",
]

# 强任务对象：出现即高度疑似需要重工具（即便没显式请求标记也升）。
# 注意：动作动词（写/改/运行…）必须与对象相邻，避免“写了代码”被强匹配命中。
_STRONG_OBJECTS = [
    r"写(个|一段|一个|点|一下)?(代码|脚本|函数|程序|爬虫|bot|机器人|工具|服务)",
    r"(改|编辑|修改|重构|修复|调试|实现|优化)(一下|这个|该|下|代码|文件|bug|功能|模块|逻辑)",
    r"(运行|执行|跑|部署|编译|构建|安装|启动|停止|kill|停).{0,6}(命令|脚本|服务|程序|容器|docker|进程|项目)",
    r"(项目|仓库|代码库|文件夹|目录|工程).{0,6}(结构|搭建|初始化|创建|脚手架|架构)",
    r"(初始化|搭建|创建|新建|生成|构建|搞|弄).{0,6}(项目|仓库|工程|脚手架|服务)",
    r"(前端|后端|api|接口|数据库|sql|html|css|javascript|typescript|python|java|go|rust)",
    r"(bug|error|报错|异常|崩溃|故障|堆栈|stack)",
    r"(git|commit|push|pull|pr|分支|合并|merge|rebase)",
]

# 弱任务对象：必须搭配请求标记才升。
_SOFT_OBJECTS = [
    r"文件", r"代码", r"程序", r"脚本", r"项目", r"配置", r"函数", r"模块", r"仓库",
]

# 否定句（如“不要写代码”“别帮我改文件”）保持 chat，不升级。
_NEGATORS = ["不要", "别", "不用", "不需要", "别帮我", "我不想", "暂时不用", "先不"]


def _enabled() -> bool:
    return os.getenv("INTENT_ESCALATION", "1").strip() not in ("0", "false", "False", "no")


def _has_marker(text: str) -> bool:
    return any(m in text for m in _REQUEST_MARKERS)


def _has_negation(text: str) -> bool:
    return any(n in text for n in _NEGATORS)


def _strong_match(text: str) -> bool:
    return any(re.search(p, text) for p in _STRONG_OBJECTS)


def _soft_match(text: str) -> bool:
    return any(re.search(p, text) for p in _SOFT_OBJECTS)


def classify_intent(text: str) -> str:
    """把用户消息分类为 ``"chat"`` 或 ``"coding"``。

    返回 ``"coding"`` 表示疑似需要文件 / 命令类重工具，后端应把会话升到 coding；
    否则返回 ``"chat"``（默认轻量模式）。
    """
    if not text or not text.strip():
        return "chat"
    if not _enabled():
        return "chat"
    t = text.strip().lower()
    # 否定句直接留 chat（即便含强对象也尊重用户“不要做”的意图）
    if _has_negation(t):
        return "chat"
    # 强对象：直接判定任务
    if _strong_match(t):
        return "coding"
    # 弱对象 + 请求标记：判定任务
    if _soft_match(t) and _has_marker(t):
        return "coding"
    return "chat"
