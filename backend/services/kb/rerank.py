"""交叉编码器重排（D55，2026-10-20）。

两步检索的第二步：
  ① 召回（`query._recall` + `_vector_recall`）只负责"别漏"，代价是查询与文档
     **各算各的**（BM25 词面 / 双塔向量），不理解"这篇到底答没答这个问题"；
  ② 重排把 `(查询, 文档)` **拼成一对**喂进 cross-encoder，直接打分，再排序。

模型：`bge-reranker-v2-m3`（基座就是 bge-m3，多了个分类头，2.14 GB）。
A/B（50 条评测集，同 kb-eval 口径）：池 30 纯重排 recall 0.8833→0.9267、MRR 0.6151→0.8273。

设计约束：
- 打分只用于**同一个候选集合内部**的排序，绝对分值不跨集合比较（中文对偶的 logit
  常常是负的，分档阈值没有意义）；
- 模型不可用（目录缺失 / 无 torch / 显存不足 / `KB_RERANK=0`）→ 返回 None，
  调用方**退回不重排**，且不因降级而改变召回池（否则会静默变差）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
from backend.config import deployment

MODEL_DIR = Path(os.getenv("RERANK_MODEL_DIR") or PROJECT_ROOT / ".models" / "bge-reranker-v2-m3")

# 召回池 = top_k × 该倍数（A/B：30 条时 recall 与 MRR 双升；10 条只能提升 MRR）
RERANK_POOL = 3
# 每条候选送进模型的文本长度上限（标题 + 命中片段）
MAX_DOC_CHARS = 1200
BATCH = 16

_cache: dict = {"tokenizer": None, "model": None, "device": None, "failed": False}


def enabled() -> bool:
    """是否启用重排（`KB_RERANK=0` 全局关闭，供对照实验/无 GPU 机器）。"""
    return os.environ.get("KB_RERANK", "1") not in ("0", "false", "False")


def available() -> bool:
    """重排是否**用得了**：开关开着、模型文件在、依赖导入得动。不加载权重。"""
    if not enabled():
        return False
    if _cache["failed"]:
        return False
    if not (MODEL_DIR / "config.json").is_file():
        _cache["failed"] = True
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except Exception:  # noqa: BLE001
        _cache["failed"] = True
        return False
    return True


def _load():
    if _cache["model"] is not None:
        return _cache["model"]
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    _cache.update(tokenizer=tokenizer, model=model, device=device)
    return model


def doc_text(group: dict) -> str:
    """送进 reranker 的文档文本：标题 + 命中片段（与 A/B 实验一致）。"""
    title = group.get("title") or ""
    snippet = group.get("snippet") or ""
    return (f"{title} {snippet}").strip()[:MAX_DOC_CHARS]


def score_pairs(query: str, texts: list[str]) -> list[float] | None:
    """逐对打分；模型不可用返回 None。"""
    if not texts or not available():
        return None
    try:
        import torch

        model = _load()
        tokenizer = _cache["tokenizer"]
        device = _cache["device"]
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(texts), BATCH):
                batch = texts[start:start + BATCH]
                encoded = tokenizer(
                    [query] * len(batch), batch, return_tensors="pt",
                    truncation=True, max_length=512, padding=True,
                ).to(device)
                scores.extend(float(x) for x in model(**encoded).logits.view(-1).cpu())
        return scores
    except Exception:  # noqa: BLE001 — 显存/精度任何异常都退化为不重排
        _cache["failed"] = True
        return None


def rerank_groups(query: str, groups: list[dict], top_k: int, *,
                  scorer=None) -> tuple[list[dict], dict]:
    """按重排分排序并截前 `top_k`；失败则原样截断。

    返回 `(groups, stats)`。`scorer` 供单测注入，签名 `(query, texts) -> list[float]|None`。
    """
    stats = {"enabled": False, "model": MODEL_DIR.name, "pool": len(groups),
             "returned": min(top_k, len(groups)), "seconds": 0.0, "error": None}
    if not groups:
        return [], stats
    started = time.perf_counter()
    fn = scorer or score_pairs
    try:
        scores = fn(query, [doc_text(group) for group in groups])
    except Exception as exc:  # noqa: BLE001
        scores = None
        stats["error"] = f"{type(exc).__name__}: {exc}"
    if not scores or len(scores) != len(groups):
        stats["seconds"] = round(time.perf_counter() - started, 3)
        stats["error"] = stats["error"] or "reranker unavailable"
        return groups[:top_k], stats

    order = sorted(range(len(groups)), key=lambda i: (-scores[i], i))
    picked = []
    for index in order[:top_k]:
        group = dict(groups[index])
        group["rerank_score"] = round(float(scores[index]), 4)
        picked.append(group)
    stats.update(enabled=True, seconds=round(time.perf_counter() - started, 3))
    return picked, stats
