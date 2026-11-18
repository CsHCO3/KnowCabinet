# -*- coding: utf-8 -*-
r"""下载 BAAI/bge-reranker-v2-m3 到本地 .models 目录（与 bge-m3 同约定）。

用法（在仓库根目录运行）：
    python scripts/fetch_reranker.py
    python scripts/fetch_reranker.py --smoke

设计要点：
- 目标目录为项目根目录下的 .models/bge-reranker-v2-m3
- 依次尝试 hf-mirror.com → huggingface.co；镜像挂了自动换官方
- 断点续传：中断后重跑会跳过已下好的文件（.incomplete 会续传）
- 只拉推理必需文件，不拉 onnx/ 和 pytorch_model.bin（省 ~2 GB）
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ID = "BAAI/bge-reranker-v2-m3"
DEST = Path(__file__).resolve().parents[1] / ".models" / "bge-reranker-v2-m3"
ENDPOINTS = ["https://hf-mirror.com", "https://huggingface.co"]

# xlm-roberta-large 基座：safetensors 权重 + 分词器即可，不需要 onnx/ 与 .bin 双份
ALLOW = [
    "*.json",
    "*.txt",
    "*.model",            # sentencepiece.bpe.model
    "*.safetensors",
    "*.md",
    "1_Pooling/*",
]


def _hf():
    from huggingface_hub import snapshot_download
    return snapshot_download


def attempt(endpoint: str, retries: int = 3) -> bool:
    snapshot_download = _hf()
    for i in range(1, retries + 1):
        try:
            print(f"[try {i}/{retries}] {endpoint}")
            t0 = time.time()
            snapshot_download(
                repo_id=REPO_ID,
                local_dir=str(DEST),
                allow_patterns=ALLOW,
                endpoint=endpoint,
                max_workers=4,
            )
            print(f"[ok] {endpoint} 用时 {time.time() - t0:.1f}s")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {type(e).__name__}: {str(e)[:300]}")
            if i < retries:
                time.sleep(3 * i)
    return False


def verify() -> bool:
    need = ["config.json", "model.safetensors", "tokenizer.json", "sentencepiece.bpe.model"]
    missing = [n for n in need if not (DEST / n).is_file()]
    if missing:
        print(f"[verify] 缺文件: {missing}")
        return False
    size = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file())
    print(f"[verify] ok, 共 {size / 2**30:.2f} GB @ {DEST}")
    return True


def smoke() -> None:
    from sentence_transformers import CrossEncoder

    m = CrossEncoder(str(DEST), max_length=512)
    pairs = [
        ("知识分级怎么定义的", "本文档说明 tier 的取值：0=线索 1=原文 2=轻整理 3=提炼。"),
        ("知识分级怎么定义的", "今天天气不错，适合出去散步。"),
    ]
    scores = m.predict(pairs)
    print("[smoke] scores:", [round(float(s), 4) for s in scores])
    print("[smoke] 期望：第 1 条明显高于第 2 条" if scores[0] > scores[1] else "[smoke] 警告：排序异常")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="下载后跑一次打分自测")
    ap.add_argument("--verify-only", action="store_true", help="只检查文件是否齐全")
    args = ap.parse_args()

    if args.verify_only:
        return 0 if verify() else 1

    DEST.mkdir(parents=True, exist_ok=True)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    print(f"[env] HF_ENDPOINT={os.environ.get('HF_ENDPOINT')}  HTTPS_PROXY={proxy}")
    print(f"[dest] {DEST}")

    for ep in ENDPOINTS:
        if attempt(ep):
            break
    else:
        print("\n[失败] 两个源都不可达。排查：")
        print("  1) VPN/代理是否真的生效：curl -I https://hf-mirror.com")
        print("  2) Clash 需同时设环境变量：$env:HTTPS_PROXY='http://127.0.0.1:7890'")
        return 2

    if not verify():
        return 3
    if args.smoke:
        smoke()
    return 0


if __name__ == "__main__":
    sys.exit(main())
