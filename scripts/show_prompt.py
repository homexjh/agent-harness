#!/usr/bin/env python3
"""B-lite 贡献器管线 · 本地验证脚本

不启服务、不改代码，直接复刻 graph.context_node 的 system-prompt 拼装逻辑，
把每个贡献器产出的片段和最终拼接结果打印出来——一眼看清 B-lite 是否生效。

用法:
    PYTHONPATH=<项目根> python scripts/show_prompt.py
（推荐用隔离 venv 的 python，见 README）
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.server.prompt_contributors import (  # noqa: E402
    PromptContext,
    SyncPromptContributor,
    get_prompt_manager,
)
from src.server.core_files import get_core_files_manager  # noqa: E402


def main() -> None:
    user_id = "default"
    thread_id = "demo-thread"
    last_user_text = "今天上海天气怎么样？帮我查一下"

    # workspace 文件管理器（AGENTS/SOUL/PROFILE…）；memory 可选，拿不到就跳过
    cfm = get_core_files_manager()
    # 确保内置模板已写入（initialize_templates 幂等：仅写缺失/0 字节文件，
    # 已存在的用户文件不触碰）。历史上 initialize_templates 未被自动调用，
    # 会导致 workspace 文件缺失、贡献器跳过——这里补齐以便完整展示。
    try:
        cfm.initialize_templates("zh")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 初始化 workspace 模板失败: {exc}")
    mm = None
    try:
        from src.server.graph_provider import get_memory_manager
        from src.server.config import get_config
        from src.server.prompt_contributors import is_multimodal_model

        mm = get_memory_manager(user_id)
        model_name = (get_config().llm.get("model") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] memory_manager 不可用，Memory 贡献器将被跳过: {exc}\n")
        model_name = ""
        is_multimodal_model = lambda m: False  # noqa: E731

    pm = get_prompt_manager()
    pctx = PromptContext(
        user_id=user_id,
        thread_id=thread_id,
        messages=[],
        last_user_text=last_user_text,
        core_files_manager=cfm,
        memory_manager=mm,
        mode_hint="# Agent Mode\n（示例 mode hint，由图在运行时注入）",
        model_name=model_name,
    )

    print("=" * 78)
    print(f"当前模型: {model_name or '(空)'}"
          f"  →  多模态(视觉): {'是' if is_multimodal_model(model_name) else '否'}")
    print("  （multimodal_hint 仅在模型支持视觉时注入截图/view_image 指引）")
    print("=" * 78)
    print()

    print("=" * 78)
    print("已注册贡献器（按 priority 升序）:")
    print("  " + " → ".join(pm.names()))
    print("=" * 78)
    print()

    for c in sorted(pm._contributors, key=lambda x: x.priority):
        try:
            frag = (
                c.contribute_sync(pctx)
                if isinstance(c, SyncPromptContributor)
                else None
            )
        except Exception as exc:  # noqa: BLE001
            frag = f"<error: {exc}>"
        print(f"### [{c.priority:>3}] {c.name}")
        if frag and frag.strip():
            print(frag)
        else:
            print("  (None / 跳过)")
        print()

    final = pm.build_sync(pctx)
    print("#" * 78)
    print("最终拼接的 system prompt:")
    print("#" * 78)
    print(final)
    print("#" * 78)
    print(f"总长度: {len(final)} 字符")
    print()
    print("提示: 想看「关掉某个贡献器」的效果，可在上面修改 PromptConfig")
    print("      （enable_env_context=False 等），再重跑本脚本。")


if __name__ == "__main__":
    main()
