"""ragsearch 配置：知识库注册表加载 + 默认参数。"""
import json
import os

# playwright/ 根目录
_PLAYWRIGHT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REGISTRY_PATH = os.path.join(_PKG_DIR, "registry.json")

# 默认参数
DEFAULT_TOP_K = 10
BM25_TOP_K = 50
TITLE_WEIGHT = 0.35
BODY_WEIGHT = 0.65
SNIPPET_CONTEXT = 120
SNIPPET_MAX = 300
HIGHLIGHT = True

# 索引根目录（每库一个子目录）
INDEX_ROOT = os.path.join(_PKG_DIR, "index")

# 预留：嵌入模型端点（Ollama HTTP 方式）
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# 重排后端：pass-through（默认）| cosine（bge-m3 本地 FlagEmbedding）
RERANK_BACKEND = os.environ.get("RERANK_BACKEND", "pass-through")
# 本地嵌入模型名（FlagEmbedding 用）
BGE_MODEL = os.environ.get("BGE_MODEL", "BAAI/bge-m3")

# LLM 查询改写（OpenAI 兼容接口，默认 DeepSeek）
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
# 检索前是否改写查询：off（默认）| on
QUERY_REWRITE = os.environ.get("QUERY_REWRITE", "off")


def load_registry(path: str = None) -> dict:
    """读取 registry.json；相对路径以 playwright/ 根为基准解析为绝对路径。

    条目支持字符串路径，或 {"path"/"dir": 路径, ...可选设置}。
    只返回 name → 绝对路径。
    """
    if path is None:
        path = _REGISTRY_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    kbs = {}
    for name, entry in raw.items():
        rel = entry if isinstance(entry, str) else entry.get("path", entry.get("dir", ""))
        kbs[name] = rel if os.path.isabs(rel) else os.path.join(_PLAYWRIGHT_DIR, rel)
    return kbs


def load_kb_settings(path: str = None) -> dict:
    """读取 registry.json 中每库的可选设置（title_weight/tag_boost/include/exclude 等）。"""
    if path is None:
        path = _REGISTRY_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    settings = {}
    for name, entry in raw.items():
        if isinstance(entry, dict):
            settings[name] = {k: v for k, v in entry.items() if k not in ("path", "dir")}
    return settings


KNOWLEDGE_BASES = load_registry()
KB_SETTINGS = load_kb_settings()
