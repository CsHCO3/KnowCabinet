#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小 LLM 调用器（从 infosystem 的 backend/core/llm/client.py 裁剪而来）。

只保留「文本同步调用」这条路径：requests POST OpenAI 兼容的 chat/completions。
没有 agent、没有工具调用、没有重试编排之外的任何逻辑。

来源与改造说明见 MERGE_BACK.md。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests


@dataclass
class LLMConfig:
    endpoint: str = "https://api.deepseek.com/v1/chat/completions"
    api_key: str = ""
    model: str = "deepseek-chat"
    max_retries: int = 3
    retry_base_delay: float = 2.0
    temperature: float = 0.3
    max_tokens: int = 3000
    timeout: int = 300
    request_template: dict = field(default_factory=dict)


def _resolve(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("ENV:"):
        return value
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[5] / ".envs", interpolate=False)
    name = value[4:].strip()
    key = os.getenv(name, "").strip()
    if not key:
        raise RuntimeError(f"请在 .envs 配置 {name}")
    return key


def load_config(path: Path | str, model_key: str | None = None) -> LLMConfig:
    """读取 llm_config.json；支持 {'models': {key: {...}}} 或直接 {key: {...}}。"""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    models = raw.get("models", raw)
    key = model_key or raw.get("default") or next(iter(models))
    cfg = dict(models[key])
    out = LLMConfig(
        endpoint=cfg.get("endpoint", LLMConfig.endpoint),
        api_key=_resolve(cfg.get("api_key", "")),
        model=cfg.get("model", key),
        max_retries=int(cfg.get("max_retries", 3)),
        retry_base_delay=float(cfg.get("retry_base_delay", 2.0)),
        temperature=float(cfg.get("temperature", 0.3)),
        max_tokens=int(cfg.get("max_tokens", 3000)),
        timeout=int(cfg.get("timeout", 300)),
        request_template=cfg.get("request_template") or {},
    )
    out.model_key = key  # type: ignore[attr-defined]
    return out


class LLMClient:
    """同步文本调用，带指数退避重试。"""

    def __init__(self, config: LLMConfig):
        self.config = config

    def call(self, messages: list[dict], max_tokens: int | None = None,
             temperature: float | None = None) -> str:
        c = self.config
        payload = {
            "model": c.model,
            "messages": messages,
            "stream": False,
            "temperature": c.temperature if temperature is None else temperature,
            "max_tokens": c.max_tokens if max_tokens is None else max_tokens,
        }
        payload.update(c.request_template or {})
        headers = {"Authorization": f"Bearer {c.api_key}", "Content-Type": "application/json"}

        last: Exception | None = None
        for attempt in range(1, c.max_retries + 1):
            try:
                resp = requests.post(c.endpoint, headers=headers, json=payload, timeout=c.timeout)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                body = resp.json()
                return body["choices"][0]["message"]["content"]
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < c.max_retries:
                    time.sleep(c.retry_base_delay * (2 ** (attempt - 1)))
        raise RuntimeError(f"LLM 调用失败（重试 {c.max_retries} 次）：{last}")

    def ask(self, system: str, user: str, **kw) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        return self.call(msgs, **kw)
