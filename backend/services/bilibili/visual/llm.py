"""LLM 调用（自 finalize.py 抽出）：glm-flash 优先、deepseek 兜底，带限额熔断。"""
import json
import re
import time

import requests

from backend.config.settings import Settings

_LLM_DEAD: set[str] = set()  # 熔断：撞限额(1308)/持续挂的模型键，本进程内不再尝试


def log(msg: str) -> None:
    print(msg, flush=True)


def no_think(model_key: str) -> dict | None:
    """智谱 glm-* 的 flash 默认开思考，会把 max_tokens 烧在 reasoning 上、正文为空。
    返回给 LLMClient.call(extra_body=...) 的关闭思考参数；非 glm 模型返回 None。"""
    return {"thinking": {"type": "disabled"}} if model_key.startswith("glm") else None


def llm_call_raw(prompt: str, max_tokens: int = 4000) -> str | None:
    """glm-flash 优先、deepseek-v4-flash 兜底的健壮调用；返回文本或 None。
    429/1308（5小时限额）这类非瞬态错误直接熔断该键，避免每次白等重试。"""
    import time as _time
    from backend.config.settings import Settings
    s = Settings()
    for key in ("glm-flash", "v4flash"):
        if key in _LLM_DEAD:
            continue
        try:
            client = s.get_llm_client(key)
        except Exception:
            continue
        for attempt in range(3):
            try:
                resp = client.call([{"role": "user", "content": prompt}],
                                   max_tokens=max_tokens, temperature=0.2,
                                   extra_body=no_think(key))
            except Exception as e:
                msg = str(e)
                if "1308" in msg:  # 限额窗口未到，重试无意义
                    _LLM_DEAD.add(key)
                    log(f"  LLM[{key}] 限额熔断，转兜底")
                    break
                log(f"  LLM[{key}] 异常({attempt+1}/3): {msg[:100]}")
                _time.sleep(2 * (attempt + 1))
                continue
            if (resp or "").strip():
                return resp
            log(f"  LLM[{key}] 空正文，重试({attempt+1}/3)")
            _time.sleep(3 * (attempt + 1))
    return None


def parse_json_loose(resp: str):
    """从混着解释文字的回复里抠出第一个能解出来的 JSON 对象/数组。

    不要用 `re.search(r"\\{.*\\}")` 再 json.loads：模型若在 JSON 后面又写一段
    （或连吐两个 JSON），贪婪匹配会把两段粘在一起，报 `Extra data: line N`。
    """
    if not resp:
        return None
    text = resp.strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, (dict, list)) and obj:
            return obj
    return None


