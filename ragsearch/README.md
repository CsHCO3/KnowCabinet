# ragsearch — 检索底层库（不再是独立服务）

> **2026-10-21 收缩**：本包曾是一套自带 CLI（`python -m ragsearch.cli`）和 FastAPI 服务
> （`python -m ragsearch.server --port 8083`）的独立检索系统。两套入口已删除。
> **现在它只是被 infosystem 引用的库**，检索入口一律走 `python -m backend.main kb-ask`。

## 这个包里还剩什么

| 文件 | 作用 | 谁在用 |
|---|---|---|
| `engine/chunker.py` | md → 段落块（`chunk_md`） | `backend/services/kb/vector.py` |
| `engine/bge_embed.py` | bge-m3 向量化（`embed_dense`） | `kb/vector.py`、`kb/query.py` |
| `engine/document.py` | frontmatter/标题/标签解析（`parse_frontmatter`/`parse_tags`） | `kb/catalog.py`、`archive` |
| `engine/indexer.py` | BM25 文档索引（`DocIndexer`） | `backend/services/ragsearch/service.py`（含 `kb-eval` 的旧基线） |
| `engine/filter.py`、`engine/dedup.py`、`engine/reranker.py`、`engine/snippet.py` | 过滤/去重/重排/摘要片段 | 同上 |
| `config.py` | 库路径、权重、registry 读取 | 多处 |
| `registry.json` | kb 名 → 目录 映射（kb 扫描仍读它） | `kb/catalog.py` |

**已删除**：`cli.py`、`server.py`、`engine/query_rewriter.py`、`engine/dense_search.py`、
`engine/build_index.py`、`engine/vectorstore.py`、`index/vector/**`（FAISS，150.9 MB）、
`index/{_tmp,docs,1-3-VERA,3-IMA,…}`（废弃库索引）、对应单测。

## 现在怎么用

```bash
# 检索（唯一入口）
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-ask "问题" --pretty
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-fetch <doc_id> --pretty

# 内容变更后重建派生层
... kb-scan     # 目录层 + 块
... kb-embed    # 增量向量（kb.db 的 kb_vec）

# 任意文件夹临时 BM25 检索（不进库）
... rag-folder <文件夹> "问题"
```

`index/<库>/bm25_index.pkl` 仍然在役：`kb-eval` 用它算旧基线，`archive`/`condense`/
`research_project` 入库后会自动 `rebuild_kb`。**不要删**，也**不要往 `index/vector/` 里写东西**
（那层已经不在了）。

完整用法见 `文档以及其它代码/(4)RAG与信息整合设计/RAG用法速查.md`。
