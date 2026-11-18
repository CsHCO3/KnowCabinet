"""bge-m3 本地嵌入封装（FlagEmbedding）。模型首次调用时加载，进程内单例。

环境变量:
    BGE_MODEL   已获取权重的本地目录或显式指定的模型名
    BGE_DEVICE  cuda | cpu（默认自动检测）
    HF_ENDPOINT HuggingFace 镜像（国内建议 https://hf-mirror.com）

默认使用项目根的 `.models/bge-m3/`。缺失时请准备权重或设置 BGE_MODEL。
"""

import os
import threading

from backend.config import deployment

BGE_MODEL = os.environ.get("BGE_MODEL", "")

# playwright 根目录下本地模型目录（local_dir 方式，无符号链接）
_PLAYWRIGHT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BGE_MODEL_DIR = os.path.join(_PLAYWRIGHT_DIR, ".models", "bge-m3")

_model = None
_lock = threading.Lock()


def _resolve_model_path() -> str:
    if os.environ.get("BGE_MODEL"):
        return BGE_MODEL
    if os.path.isdir(BGE_MODEL_DIR):
        return BGE_MODEL_DIR
    raise FileNotFoundError("请准备 .models/bge-m3，或在 .envs 配置 BGE_MODEL")


def _pick_device() -> str:
    dev = os.environ.get("BGE_DEVICE")
    if dev:
        return dev
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def get_model():
    """惰性加载 BGEM3FlagModel 单例。"""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from FlagEmbedding import BGEM3FlagModel
                _model = BGEM3FlagModel(_resolve_model_path(), use_fp16=True, device=_pick_device())
    return _model


def embed_dense(texts: list) -> "np.ndarray":
    """返回 dense 向量矩阵，形状 (len(texts), 1024)。"""
    model = get_model()
    # batch 加大到 128（原 16）：小 batch 导致 GPU 大量时间花在 kernel 启动/拷贝开销上。
    # 对 3060 6GB + bge-m3 fp16，128 是安全上限；可用 BGE_BATCH 覆盖。
    batch = int(os.environ.get("BGE_BATCH", "128"))
    out = model.encode(texts, batch_size=batch, max_length=512)
    return out["dense_vecs"]


def reset():
    """重置单例（测试用）。"""
    global _model
    _model = None
