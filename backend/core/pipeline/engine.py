"""Pipeline 编排器 — 按拓扑序执行 StepDef 列表，管理 Context。"""

import time
import logging

from .step import StepDef, Context, RetryPolicy
from backend.core.llm.client import LLMClient

logger = logging.getLogger("infosystem.pipeline")


class PipelineEngine:
    """顺序执行 steps，支持重试和条件跳过。

    用法:
        engine = PipelineEngine(steps=[...], llm=llm)
        ctx = engine.run({"url": "https://..."})
        print(ctx["result"])
    """

    def __init__(self, steps: list[StepDef], llm: LLMClient | None = None):
        self.steps = steps
        self.llm = llm

    def run(self, initial: Context | None = None) -> Context:
        ctx: Context = dict(initial or {})

        for step in self.steps:
            if step.skip_if and step.skip_if(ctx):
                logger.info("[%s] skipped", step.name)
                continue

            logger.info("[%s] running (inputs=%s)", step.name, step.inputs)

            if step.retry:
                result = self._run_with_retry(step, ctx)
            else:
                result = step.handler(ctx, self.llm)

            if step.outputs and result is not None:
                if len(step.outputs) == 1:
                    ctx[step.outputs[0]] = result
                else:
                    for k, v in zip(step.outputs, result if isinstance(result, (list, tuple)) else [result]):
                        ctx[k] = v

            logger.info("[%s] done (outputs=%s)", step.name, step.outputs)

        return ctx

    def _run_with_retry(self, step: StepDef, ctx: Context) -> object:
        last: Exception | None = None
        for attempt in range(1, step.retry.max_attempts + 1):
            try:
                logger.info("[%s] attempt %d/%d", step.name, attempt, step.retry.max_attempts)
                return step.handler(ctx, self.llm)
            except Exception as exc:
                last = exc
                if attempt < step.retry.max_attempts:
                    delay = step.retry.delay(attempt)
                    logger.warning("[%s] attempt %d failed: %s; retry in %.0fs",
                                   step.name, attempt, exc, delay)
                    time.sleep(delay)
        raise RuntimeError(f"[{step.name}] all {step.retry.max_attempts} attempts failed") from last
