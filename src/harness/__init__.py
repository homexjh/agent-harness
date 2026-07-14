"""harness 包：基于 LangGraph 的 Agent 可靠性层（路线 A）。

把 QwenPaw 的 Loop Gates / StopHandler 思想，用 LangGraph 图节点重新实现。
Governor 是真·图节点，跑在每轮"模型调用 + 工具执行"的干净边界上。
"""
