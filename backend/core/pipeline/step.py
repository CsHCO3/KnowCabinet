"""Step 定义。"""

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.core.llm.client import LLMClient


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff: float = 2.0

    def delay(self, attempt: int) -> float:
        return self.backoff * (2 ** (attempt - 1))


Context = dict[str, Any]
"""Pipeline 上下文 — steps 通过它读取和写入数据。"""

StepHandler = Callable[[Context, LLMClient | None], Any]
"""Handler 签名：(ctx, llm) → 任意值（写入 ctx[outputs]）。"""


@dataclass
class StepDef:
    """Pipeline 中的一步。

    - handler(ctx, llm) 执行逻辑，返回值写入 outputs。
    - inputs / outputs 声明上下文键名，编排器据此做数据流映射。
    - retry 启用自动重试；skip_if 在条件满足时跳过本步。
    """

    name: str
    handler: StepHandler
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] | None = None
    retry: RetryPolicy | None = None
    skip_if: Callable[[Context], bool] | None = None
