"""工具定义 — 统一的 Function Calling Schema。

每个 Tool 携带 name / description / parameters (JSON Schema) / handler，
可供 LLM 调用侧注册，也可被 Pipeline step 直接执行。
"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                     # JSON Schema dict
    handler: Callable[..., Any]          # 实际执行函数
    required: list[str] = field(default_factory=lambda: ["input"])

    def to_openai_tool(self) -> dict:
        """转为 OpenAI tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }

    def execute(self, **kwargs) -> Any:
        return self.handler(**kwargs)
