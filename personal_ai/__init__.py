"""Personal AI — 统一 AI 入口与个人 AI 数据层。

设计原则（来自 _agent_center/PROJECT_CONTEXT.md）：
1. History / Memory / Task / 项目上下文属于用户，不属于任何模型。
2. Model / Agent / Provider 三层都可以替换。
3. 来源透明：回答必须携带真实的调用链，而不是只显示模型名。

当前阶段：A1（后端骨架 + Provider 适配层）。
"""

__version__ = "0.1.0-a1"
