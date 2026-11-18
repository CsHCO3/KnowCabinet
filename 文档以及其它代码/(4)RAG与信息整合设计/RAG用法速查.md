# 检索用法速查（infosystem）

> 纯命令速查。**2026-10-21 改版**：旧的 `rag-ask` / `ragsearch-index` / `/rag` 问答页 / FAISS 向量层
> 已全部下线删除，检索只剩 `kb-ask` / `kb-fetch` 一条路（读 `data/kb/kb.db`）。
> 运行目录：`D:\infosystem`，入口 `python -m backend.main <cmd>`，Python 用 `D:/Anaconda/envs/infosystem/python.exe`。

## 检索（唯一入口）
```bash
# 查片段：每条带 doc_id / 版本(tier) / chunk_ids / 片段
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-ask "问题" --pretty \
    [--topic papers|cbac|dairy|campus|ai-agent|videos|research-projects] \
    [--mode browse|balanced|deep] [--vector] [--top-k N] \
    [--tier-min 0 --tier-max 3] [--org 实验室内] [--year 2024] [--source-type paper] [--tags a,b]
```

- `balanced`（默认）只走词法；`--mode deep --vector` 才开语义支路（跨语言、换说法）。
- **不做 LLM 总结**——只给片段。要成段回答，由调用方（agent 或人）自己读片段/取原文。

## 取原文
```bash
# 整篇最高档正文
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-fetch <doc_id> --pretty
# 只取命中块 ±1（chunk_ids 来自 kb-ask 输出）
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-fetch <doc_id> --tier 1 --chunk-ids 6106,6221 --pretty
```

## 内容变更后重建索引
```bash
# 新内容先落进 knowledgebase/（分区目录），再：
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-scan      # 重建目录层与块（约 3 分钟）
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-embed     # 只补新增块的向量
D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-inventory # 体检
```

## 多平台搜论文（含 --merge 合并去重）
```bash
python -m backend.main paper-search "关键词" --rows N [--merge --keywords ... --top-n N --export-json ...]
```

## 定向下载
```bash
python -m backend.main download-papers --file 清单.txt --import-meta
```

## 任意文件夹临时检索（不进库）
```bash
python -m backend.main rag-folder <文件夹> "问题" [--top-k N] [--group]
```

## 看有哪些库 / 分区
```bash
# 检索分区（topic）与档位定义
cat knowledgebase/_meta/config/topics.json
cat knowledgebase/_meta/config/tiers.json
# 库路径映射（kb 名 → 目录），kb 扫描仍读它
cat ragsearch/registry.json
```

## 要点
- **一篇文档只有一个 topic**；跨分区撞名时用 `topic:id` 指定。
- `knowledgebase/` 是唯一真相源；`data/kb/kb.db` 是可随时重建的派生层（删了不影响知识）。
- `data/` 里只放二进制原稿与流水线中间物，**不参与检索**。
- 每次 `kb-ask` 是新进程，会加载 2 GB 重排模型（首次约 40 秒，不是卡死）；批量查询写脚本一次跑完。
