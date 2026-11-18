# infosystem 命令速查（权威活文档）

> 本文件是 `infosystem` CLI 工具的**唯一权威速查**，skill `infosystem` 直接引用本绝对路径，不复制内容。
> 遇到本文件未覆盖的新坑，按文末「补记协议」追加。

## 入口（重要）

```bash
cd D:\infosystem
python -m backend.main --help        # 枚举全部子命令
python -m backend.main <cmd> --help  # 单个命令的完整参数（最权威，本文件可能滞后）
```
如果你用rag系统，建议试试"D:\infosystem\文档以及其它代码\(4)RAG与信息整合设计\RAG用法速查.md"

> ⚠️ **`python run.py` 已失效**（run.py 已删除，根目录只剩 `run_api.py`，那是 API 服务不是 CLI）。README 里所有 `python run.py ...` 都要换成 `python -m backend.main ...`。

环境：Python 3.12，依赖在 `backend/requirements.txt`。LLM API key、B站 cookie、SSH 参数都在项目根 `.env`；模型列表在 `backend/config/model_config.json`。该文件不入库，首次使用先复制模板：`Copy-Item backend\config\model_config.example.json backend\config\model_config.json`，再在 `.env` 里填对应的 API key（模板里的 api_key 是 `ENV:` 引用）。

## Web 前端（浏览器界面，与 CLI 并行、需另起进程）

infosystem 有**两套独立接口，互不依赖**：

| 接口 | 入口 | 是否需额外进程 |
|---|---|---|
| **CLI**（`--help` 现有 56 个子命令；下方按域分节，社区来源 `tieba`/`qq`/`backlog` 在文末单独成节） | `python -m backend.main <cmd>` | **不需要**，直接跑，与 web 无关 |
| **Web 前端**（37 个 API + 13 页面） | `bash server.sh start` 启动后浏览器开 `http://127.0.0.1:8765/` | **必须先启动**，否则 web 完全不可用 |

- **没有"浏览器进程"**——`run_api.py` 就是服务器进程（uvicorn），浏览器只是客户端。不开它，`8765` 无响应（curl 返回 000）。
- 启动（推荐）：`cd D:\infosystem && bash server.sh start|stop|restart|status|log`（后台托管，日志 `data/.server.log`）。
- 前台调试：`python run_api.py --reload`（Ctrl+C 关闭；`reload` 是开发模式，**托管时别开**）。
- 环境变量：`APP_PORT`（默认 8765）、`APP_WORKERS`（默认 1）、`APP_HOST`、`APP_PYTHON`。依赖 `requirements.txt`，须从项目根跑。
- 根目录启动文件只有 5 个：`run_api.py`（唯一入口，含 `--launch` 分离启动）、`server.ps1`（托管实现）、`server.sh`（bash 入口）、`start.bat`/`stop.bat`（双击）。
- **接口权威清单**：`http://127.0.0.1:8765/docs`（Swagger 自动生成）。Web 独有的能力（B站 collect/extract/query 入库工作流、任务管理、模型/提示词配置）在 CLI 里不存在。
- **远程任务已支持提交**：`/tasks` 页看/删远程任务（每 5s 自动刷新状态），`/tasks/submit` 页可提交（上传文件或选 `data/pdfs/` 已有文件、选引擎 marker/mineru、带完整选项），走与 CLI 相同链路（复用 `backend.services.pdf_remote`）。提交前会先跑远端 `ollama_cleanup.py`（清理 UNTIL=Forever 且最近 180s 无请求的 Ollama 模型）并做 GPU 显存预检（阈值 `remote_min_gpu_free_mb`，默认 6000MB），不足直接报错不上传。源文件**复制**进 job 目录（`data/remote_pdf/{job_id}/`），`data/pdfs/` 原件保留。API：`/api/pdf/engines`、`/api/pdf/submit`、`/api/pdf/local-files`、`/api/pdf/jobs/{id}`、`/api/pdf/gpu-status`。
- **OCR 前端任务管理**：`/ocr` 页（导航「监控→OCR 识别」）选 md 文件/目录 + 模型 → 后台 OCR（复用 CLI `ocr` 的 OCRService，生成 `{stem}_ocr.md` 新文件）→ 轮询进度/逐图结果。API：`/api/ocr/submit`、`/api/ocr/tasks`、`/api/ocr/task/{id}/status`、`/api/ocr/local-mds`。
- **统一任务体系**：`.data/tasks.json` 统一记录 survey 管线 + 转换 + 翻译 + OCR 任务（分阶段/持久化/可重试）。`/task-center` 页（导航「监控→任务中心」）统一管理，API：`/api/tasks/center`。survey 表单的「远程转换/标记翻译」**已真正接线**——勾选后自动 submit_batch/submit_translate 并关联远程 job。
- 用 CLI 处理 arXiv/B站/PDF 等**无需起 web**，直接跑子命令即可。

## 六大领域 × 37 个子命令（另有第 7 类：社区来源 `backlog`/`tieba`/`qq`，见文末）

### 1. arXiv 文献调研（3）

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `survey` | 一键调研：搜索→OpenAlex 引用丰富→CCF 排序→导出+下载 PDF | `--raw-query 'all:"..." AND cat:cs.CV'` `--max-papers 200` `--top-n 15` `--download`；排序权重 `--sort-citation-weight/--sort-ccf-weight/--sort-time-weight`（默认 0.5/0.3/0.2） |
| `search` | 快速搜索导出 | `--query` `--export-json` `--export-md`；`--auto-submit`=搜索+下载 top1+提交远程转换 |
| `arxiv` | 批量价值分析（LLM 读摘要打分） | `--input xxx.json` `--source-format json`（survey 输出可直接喂给它） |

输出：`output/survey/`、`output/search/`、`output/arxiv/`，PDF 在 `data/pdfs/`。

### 2. B站视频分析（14 条在线 + 4 条已下线）

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `bili-survey-stage1` | 收集视频信息（收藏夹/合集/作者） | 三选一：`--fav-id`、`--video-url`（查合集/作者）、`--mid`（作者视频，`--order {pubdate,click,stow}` 默认 click）；`--max-pages` `--max-items` `--output` |
| `bili-survey-stage2` | 丰富信息 + 评分排序 | 衔接 stage1 输出；`--enrich-only` 只丰富不评分 |
| `bili-find` | 在只有元信息的几千条视频上检索（挑片） | `"关键词 关键词"`（每个词都要命中）、`--limit`、`--min-view`、`--sort`（relevance 或 view）、`--bvid-only`（只出 BV 号，喂管道） |
| `bili-info` | 单视频速查：元信息 + 字幕概况 | `BV号或URL`、`--json` |
| `bili-visual-scan` | **画面要素提取一站跑**：字幕→分段→扫帧→看图浓缩→落库 | `BV...`（可多个）；`--scan auto\|off`（扫帧总开关，off=一条都不扫）、`--min-sub-cover`（字幕覆盖率阈值，默认 0.15）、`--thr`（只扫评分≥thr 的段，0=全片）、`--force-scan`（诊断说不用也扫）、`--no-probe-veto`（关掉"8 帧抽样否决"）、`--from {fetch,sections,scan,digest,all}`（断点续跑）、`--no-index`、`--from-stdin`（吃 `bili-find --bvid-only`）、`--list-only`（只打印要跑哪几条）、`--asr-fallback`（字幕不可用时本地 ASR 兜底，**默认关**，小时级 GPU 开销）、`--asr-model`（默认 medium）、`--asr-compute`（默认 float16） |
| `bili-visual-apply` | 把 `data/bilibili/visual/<bv>/digest.md` 落进知识库 | `--all`、`--dry`、`--no-force` |
| `bili-annotate` | 给已摘要未分类的视频补 分类/仓库链接 | `--limit`（0=全部） |
| `bili-author-mine` | 按分类聚合作者，出宝藏作者报告 | `--output` 默认 `data/bili_servey/author_mining.md` |
| `bili-author-scan` | 从收藏锚点扫作者视频：计算机类>阈值→AI 确认领域→注册表 | 见 `--help` |
| `bili-status` | **查账**：线索→已入库/跳过/待处理，按原因分组（可重试 vs 别重试） | `--limit`（每组列几条，默认 12）、`--pending N`（待处理示例条数，默认 15）、`--json` |
| `bili-unfav` | 把**已入库确认**的视频从收藏夹取消收藏 | `--from-runs`（从 `_runs.jsonl` 筛 `kb_ok` 的）、`--fav-id`、`--apply`（不加只演练） |
| `bili-asr` | 本地 ASR 单跑转写（只产字幕缓存，不跑后续；补历史/调试） | `BV...`（可多个，支持 `?p=N`）、`--model`（tiny…large-v3，默认 medium）、`--compute`（默认 float16）、`--force`（重转，旧缓存留 `.asr_old`）、`--json` |
| `bili-backfill` | **批量回刷**：把"试过但没进库、且值得重试"的视频用 ASR 兜底串行重跑，跑完自动重建索引 | `--dry`（只列靶子）、`--only BV...`、`--retry-failed`、`--timeout 秒`、`--no-index` |
| `bili-coverage` | **收藏终态对账**：每条收藏现在到哪一步（入库/隔离/未入库原因/长视频未跑） | `--csv 路径`、`--out 明细.md`、`--all`（含长视频）、`--json` |

已下线（跑会打印替代做法并退出 1）：`bilibili`、`bili-export`、`bili-analyze`、`bili-survey-stage3`。它们的活分别由 `bili-visual-scan`、`bili-survey-stage1 --fav-id`、`bili-find`+`bili-visual-scan`、`bili-visual-scan` 承接。

输出：采集 JSON `data/bili_servey/`；字幕缓存 `data/bilibili/subtitles/<bvid>[_pN].json`（旧路径 `data/bili_subtitles/` 只剩 4 个历史文件）；画面管线全在 `data/bilibili/visual/<bvid>/`（`digest.md`、`images_used/`、`sections.json`、`report_thr*.json`、`blocks_cache.json`）；跑批账 `data/bilibili/visual/_runs.jsonl`（`bili-status` 读它）。

常用链（选片 → 深挖，一条管道）：

```bash
python -m backend.main bili-find "Agent 架构" --limit 5 --bvid-only \
  | python -m backend.main bili-visual-scan --from-stdin
```

> **⚠️ 视频进知识库走哪条路（2026-10-18 定调，2026-11-05 更新）**——三条路产物不同档位，别混用：
>
> | 路子 | 命令 | 产物 | kb 里的档位 | 能搜到吗 |
> |---|---|---|---|---|
> | **一次浓缩**（只要文字） | `video-import --bvids BVxxx,...` / `--file bvids.txt` | `knowledgebase/dense/video/{bvid}.md`（+ bili.db 行 + `data/bili_subtitles/{bvid}.json`） | **tier=2（summary/condensation），必嵌** | ✅ FTS + 向量都能中 |
> | **画面+字幕结构化**（PPT/演示型，带截图） | `bili-visual-scan BVxxx` → `bili-visual-apply` | `data/bilibili/visual/<bv>/digest.md`（按时间顺序分块，截图嵌相对路径）→ `knowledgebase/community/bilibili/dense/{bvid}.md` + `dense/_frames/<bv>/` | tier=2 | ✅ 截图能回看原文 |
> | 三遍法分析（已下线） | ~~`bilibili --url ...` / `bili-analyze`~~ | `data/videos/{bvid}/analysis_*.md` | tier=1，`videos` 主题默认 `embed_l1=false` → 0 块 0 向量 | 只有 FTS 词法能中 |
>
> 结论：**只要文字就 `video-import`**（精华提示词 `prompts/bili/extract_summary.txt`，一进一出、信息密集）；**讲者在放 PPT/架构图/网页课件，就用 `bili-visual-scan`**——它会先只用字幕判断"这视频值不值得扫画面"，不值得就退化成纯字幕浓缩，不白下视频。
> 入库后必须 `kb-scan`（全库约 210–240s）→ `kb-embed`（约 2s–40s），`kb-embed --status` 应得 `pending=0 stale=0`。`bili-visual-scan` 批量跑完会自动做这两步，`--no-index` 可关。
> 已知限制：`video-import` 的摘要只吃**字幕前 8000 字**（长视频会被截断）；B站无 CC 字幕的视频（约 1/5）字幕抓取 404，只能记失败（画面管线对这类走 `--force-scan` 仍能出 digest，但分块只能靠画面）。
> **坑：LLM 摘要输出可能中途截断（2026-11-06 实测 BV15xx96LEft，984 字节断在半句）**——入库后人工看一眼 `knowledgebase/community/bilibili/dense/{bvid}.md` 是否以完整句结尾；断了重跑 video-import，或直接读 `data/bili_subtitles/{bvid}.json` 的 `text` 字段。

### 3. 远程 PDF 转换 + 翻译（5，依赖 SSH 远程 GPU 服务器）

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `pdf` | 上传 `data/pdfs/` 下 PDF 交远程 Marker 转 Markdown | `--translate`（打标记，后续需翻译）；提交前自动做 Ollama Forever 清理 + GPU 显存预检（不足直接报错）；**远程用双卡版 `convert_dual_gpu.py`**（`REMOTE_CONVERT_SCRIPT` 在 .env 指定）——自动检测每卡剩余显存按 ~7GB/worker 分摊 worker 数，双 4090 最高 6 路；原单卡 `convert.py` 保留在远程 `.bak_dual_*` |
| `mineru` | 远程 mineru 转换 pdf/pptx/docx/xlsx/图片 → Markdown（文件显式指定） | `--files a.pptx b.docx`（必填）/ `--dir 目录`（扫描）；`--method auto|txt|ocr`、`--lang`、`--backend`（默认 pipeline）、`--formula True|False`、`--table True|False`；产物 md 原样平铺 + `{stem}.tar`(仅图片)，`warmup` 收集到 `data/remote_pdf/{job_id}/`（md+tar 平级）。注：仅 `.pptx`，旧版 `.ppt` 不支持 |
| `warmup` | 环境自检 + 拉取远端结果 | 转换结果到 `data/remote_pdf/`；翻译结果原文+`cn_` 一起拉回 |
| `translate` | 远程翻译已转换的 .md | `--auto` / `--files a.md,b.md` / `--upload-md 本地路径`；`--model qwen3:8b|qwen3:32b`、`--content-type {paper,other}`、`--translate-prompt`、`--no-detect` 强制翻译 |
| `reconcile` | 盘点整理远程任务旧账：归档已转源文件、删除孤儿记录 | 自动备份 remote_jobs.json 后执行；幂等可重复跑 |

> 中文检测默认开：正文中文字符占比 >10% 自动跳过（防二次翻译）。SSH 参数在 `.env`，服务器或网络不可用时**说明原因，勿反复重试**。
>
> **文件生命周期**：`data/pdfs/`=待处理区；提交成功即把源文件**复制**进 `data/remote_pdf/{job_id}/`（自包含：源副本 + 产物 md + 图片 tar），`data/pdfs/` 原件保留；同一文件多引擎各转一份。

> 中文检测默认开：正文中文字符占比 >10% 自动跳过（防二次翻译）。SSH 参数在 `.env`，服务器或网络不可用时**说明原因，勿反复重试**。

### 4. 远程文件系统（6）

| 命令 | 用途 |
|---|---|
| `remote-fs-list <path>` | 列举远程目录 |
| `remote-fs-tree <path>` | 打印远程目录树 |
| `remote-fs-search <name>` | 精确搜索远程文件夹名 |
| `remote-fs-download <path> [-o 本地]` | 下载远程文件夹为 tar.gz/zip（按扩展名判断格式） |
| `remote-fs-download-file <path>` | 下载远程单个文件 |
| `remote-fs-download-name <name>` | 按名字定位远程文件夹并下载 |

### 5. 信息处理（4）

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `chat` | 上传文件夹全部文件+提问，AI 一次性回答 | `--input`（必填）、`-q/--question`、`--question-file`（优先级更高）、`--model`（默认 deepseek-chat）、`--search`（联网，仅部分第三方平台支持）、`--think` |
| `persona` | 从 AI 对话记录提取人物画像要点 | `--input`（必填）、`--output`（默认 `output/persona/`）、`--user-pattern/--ai-pattern`（段落标记正则）、`--story/--story-file`、`--task-prompt`、`--workers`、`--force` |
| `ocr` | 对 `.md` 内图片 OCR，把识别文字插回图片引用下方；**默认生成 `{stem}_ocr.md` 新文件**（保留原文件） | `--model {qwen-vl-max_ocr\|glm-4.6v_ocr\|paddleocr-vl1.6_ocr}`、`--file 单文件`、`--retry`、`--no-parallel`、`--inplace`（原地改）、`--min-size-kb 7`（小于该尺寸跳过=装饰图/头像）、`--no-strip-refs`（默认剔除末尾 References/参考文献 段）；图片定位优先 `{md名}/` 子目录、退回与 md 同目录（同目录会警告）；**目录模式自动跳过 `*_ocr.md`**（避免把上次产物当输入重复计数） |
| `condense` | 浓缩已转换的论文 md → `data/papers/{arxiv_id}/condensation.md`（贡献/方法/结果/局限/关键词单条目，面向检索） | `--survey <task_id>`（浓缩该调研 convert 产物）/ `--dir <md目录>`；`--model`（默认 deepseek-chat） |
| `archive` | 归档散乱产物到统一结构：`knowledgebase/full/papers/{arxiv_id}/`（md/ocr/翻译/浓缩）+ `knowledgebase/full/docs/`，二进制到 `data/papers/{id}/`，补 frontmatter + `metadata.json`；归档后自动收敛+重建索引 | 复制为主、幂等可重跑；数据组织与扩展见 `知识库与检索系统设计.md` |

### 6. 知识库/RAG + 论文检索（2026-08 新增；2026-10 起 15）

> 三层知识库：`knowledgebase/dense/`（浓缩/摘要，语义源）+ `knowledgebase/full/`（全文，BM25）+ `data/`（原始/二进制）。**两套检索并存**：BM25 词法（整篇粒度，`ragsearch/` 引擎）+ **dense 段落语义检索**（bge-m3+FAISS，跨语言/段落粒度，先 `ragsearch-index` 建索引，见下方两条命令）。

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `kb-scan` | **M1 已实现**：只读扫描 registry md、1458 论文空壳 metadata、dense、视频、QQ 群聊 → `data/kb/kb.db`（documents/versions/FTS5），并输出疑似重复清单 | `--source all\|registry\|metadata\|dense\|videos\|external`；`--force`；stdout JSON Report；kb.db 可随时重建 |
| `kb-inventory` | **M1 已实现**：按 topic/tag 输出库存体检（L0–L3、stage、source_type、tag 分布），支持缺口与最近更新视图 | `--topic cbac`、`--tag decision`、`--view snapshot\|gap\|recent`、`--days 7`、`--pretty`；snapshot 默认 JSON，`--pretty` 输出紧凑文本 |
| `kb-rebuild` | **M1 已实现**：删除并完整重建 `data/kb/kb.db`，验证目录层可自愈 | `--keep-vector`（M4 生效预留）；stdout 含 scan_report/embed_report/row_counts |
| `kb-tags` | **M2 已实现**：统计 topic 原始/DB tags、生成只源自现有 tag 的词表草案；confirmed 后安全清洗 md frontmatter（默认 dry-run、自动 .bak） | `stats\|draft\|clean --topic cbac`；draft 可 `--top-k 60 --min-count 2`；clean 加 `--apply` 才写回；输出 JSON 或 `--pretty` 摘要。**M3 修复**：写回会覆盖已存在的 `org_marks:`/`arxiv_tags:` 行（旧版会静默丢弃计划中的组织标记） |
| `kb-ask` | **M3 已实现 / M5 加向量支路**：统一检索——确定性过滤 → 分档召回（L0 只搜元信息列；deep 另开向量支路：query 嵌入一次 → chunk KNN → chunk→doc 折叠）→ 级内归一化 → 按 mode 配额合并（deep 模式每档留 1 个向量保底名额，D34）→ 证据双轨标注 | `"查询" [--topic] [--tags a,b] [--org a,b] [--source-type] [--year] [--tier-min/max 0-3] [--stage] [--mode browse\|balanced\|deep] [--top-k 10] [--fusion norm\|rrf\|interleave\|reserve] [--fold max\|top3sum] [--vector] [--no-vector] [--pretty]`。查询内部自动经 jieba 分词（中文可直接传）；`--org` 过滤项目线/组织标记（D32）。**mode 选择**：`browse` 我有什么（偏 L0/L1）/ `balanced` 默认 / `deep` 细节优先（1/1/1/7，L3 原文优先）。**跨语言**：中文 query 可直接找到英文论文（deep 模式，实测 10 条 recall@10 0.80）；deep 首次调用含 bge 加载 ~50–120s，同进程后续 0.05–0.3s。返回的 `matched_chunks[].chunk_id` / `l3_chunk_ids` 可直接喂 `kb-fetch --tier 3 --chunk-ids` |
| `kb-fetch` | **M3 已实现 / M5 加段落级取证**：取某文档某一档正文，用于"必须回源引原文"的取证；给 `--chunk-ids` 时只回命中段落 ± 上下文（**不给整篇**，02 §11.3 第 3 条） | `<doc_id> [--tier 0-3] [--chunk-ids 18928,18929] [--context 1] [--pretty]`；缺省取最高档；同档多 kind 时原文优先于译文（paper > cn_paper > paper_ocr > cn_paper_ocr）。`--chunk-ids` 来自 `kb-ask` 的 `matched_chunks[].chunk_id`/`l3_chunk_ids`，返回 `segments[]`（含 chunk_index/total_chunks/context_before/context_after/prev_chunk_id/next_chunk_id）；块不属于指定档时报 `chunk_not_in_tier`（退出码 1）。L0 空壳返回 `meta_only=true` + 「仅元数据」note（**禁止**把 abstract 当结论引用）。**裸 id 跨 topic 歧义时不静默取一条**，返回 `{error:ambiguous_doc_id, candidates:[...]}`（退出码 1），须改用 `topic:id` 形式 |
| `kb-promote` | **M5 已实现**：L2/L3 晋升校验（D27 证据双轨门槛）——L2 必须有能解析到**本人 L3 版本**的 `[L3:路径#chunk]` 回链（块号还须真实存在）；`legacy_l2` 存量 5 条 dense 豁免放行但保持 legacy_l2；L3 须有 tier=3 版本 | `<doc_id>... [--to-tier 2\|3] [--apply] [--no-strict] [--pretty]`。默认只校验（dry-run）；`--apply` 才把通过的写成 `status='active'`；默认 `strict`：任一不通过则整批不写（`--no-strict` 只写通过者）。拒绝时退出码 1 且给出 `reason=missing_backlink`/`missing_l3_version` |
| `kb-eval` | **M3 已实现 / M5 默认集扩 50 条**：检索质量评测（recall@10 / MRR@10 对旧 `search_all` 基线），逐条对比表落盘 `data/kb/eval_report_*.json` | `[--set knowledgebase/_meta/eval/eval_v2.jsonl] [--top-k 10] [--refresh-baseline] [--pretty]`。**默认集 = eval_v2.jsonl（50 条；缺失回退 v1）**，只跑 M3 那 30 条时显式 `--set knowledgebase/_meta/eval/eval_v1.jsonl`。基线结果缓存 `knowledgebase/_meta/eval/eval_baseline.json`（只补采缺失 query）。报告含 `diagnostics.relaxed_quota_recall@10`（解除配额后的召回）、`diagnostics.cross_lingual`（跨语言子集）、`diagnostics.deep_l3_gate`（deep 模式 L3≥4）。达标才退出 0 |
| `kb-embed` | **M4 已实现**：chunk manifest 增量向量（sqlite-vec 主容器）。version 级 content_hash 短路 → chunk 级 text hash 差集；只重嵌变更块；L2/L3 必嵌、L1 按 topic `embed_l1`、L0 永不 | `[--topic cbac] [--dry-run] [--status] [--reembed-stale] [--pretty]`。`--dry-run` 只算计划不调用模型；`--status` 只读盘点 pending/stale/current；默认不做全量重嵌；`--reembed-stale` 才重算 model_id 不符的旧块。模型加载 ~91s/进程；CBAC 首建 14945 chunks/389s，零改重跑 <5s（M4.log） |
| `kb-tag` | **M6 新增**：给指定文档增删 tags。**只写 kb.db（documents.tags + kb_fts），绝不改 md 文件**（`md_written` 恒为 0）；按各 doc 自身 topic 的 confirmed 词表把 alias 归一为 canonical；写后自动同步 FTS，立刻可被 `kb-ask --tags` 命中 | `<doc_ids 逗号分隔> [--add a,b] [--remove c] [--pretty]`。未知 id 退出码 1（`not_found`）、跨 topic 歧义裸 id 返回 `ambiguous_doc_id`+候选清单；`add`/`remove` 至少要给一个。改 md frontmatter 请用 `kb-tags clean`，两者别混 |
| `kb-batch` | **M6 补齐**（02 §7 早写、M1–M5 未实现）：批次（候选区）生命周期 `open/add/confirm/list`——外搜结果先攒进候选区（不影响检索），用户确认后一次性写 topic + 打 tags（D12）。写操作走 `db.write_retry` 短事务 + busy 重试 3 次 | `open [--round-id] [--topic]` / `add ROUND_ID DOC_ID...` / `confirm ROUND_ID --topic T [--tags a,b]` / `list [--status open\|confirmed]`；均支持 `--pretty`。`add` 对不在库的 id 返回 `unknown_doc`（不静默吞）；`confirm` 空批 `empty_round`、重复确认 `already_confirmed`；返回体含 `tags_applied`/`written_docs`/`tag_report` |
| `rag-ask` | 纯检索（给 AI 用，不整合 LLM）：跨库返回命中片段+元信息；指定论文取全文 | `"查询" [--kb dense\|papers\|all] [--top-k N]`；`--papers 2210.04644,1807.10953` 取指定论文全文；**`--dense` 切到 bge-m3 段落语义检索**（跨语言/段落粒度，须先 `ragsearch-index`，且**须用 conda `infosystem` 环境跑**）；`--tags 实验室内` `--section` `--source-type` 过滤（BM25 与 dense 均支持） || `ragsearch-index` | 构建 dense 向量索引（bge-m3+FAISS，段落级）：知识库 md → 分块 → 嵌入 → 存 `ragsearch/index/vector/{kb}/vs` | `--kb papers`（默认 papers，也支持 docs/runoob/newapi/dense）；`--no-rebuild` 有现成索引直接加载；`--list-kbs` 列出已登记库；**须用 conda `infosystem` 环境跑**（faiss/bge 只装在那） |
| `rag-folder` | 任意文件夹立刻 BM25 检索（不向量化/不进知识库/纯 AI 用），临时索引缓存 | `<文件夹> "查询" [--top-k] [--section/--tags/--year/--source-type] [--group]`；文件夹格式=内含 .md（递归/UTF-8，frontmatter 可选） |
| `fill-meta` | 补齐论文缺失元信息（确定性清洗引擎：字段级信任 + provenance + manual_fields + section/tags） | `--papers 1810.01291,2302.08496` 指定 / `--doi 10.xxx` 按 DOI 解析；**批量须显式 `--all`**（扫描 full/papers 缺 authors/year 的）；`--dry-run` 只列不改。**安全：无参数不扫描**；对非 arxiv_id/DOI-slug 目录名跳过解析；`_ensure_metadata_paper` 增量合并不丢 source/is_oa 字段 |
| `add-paper` | 从 DOI 或 arxiv_id 创建论文 KB 元数据条目（无文件也行） | `--doi 10.xxx/yyy` 或 `--arxiv 2302.08496` |
| `import-papers` | 从外部论文文件夹导入（md+pdf+图，防丢；copy 不 move 源） | `--src <文件夹>`（含 `{arxiv_id}_{title}.md/.pdf/图子目录`） |
| `rag-migrate` | 库↔库批量迁移论文（copy/move，目录级整体搬），用于通用库↔专用库/项目库之间的文献移动 | `--from <库> --to <库>`（registry.json 里的 key）；筛选 `--ids` / `--tags`（任一命中）/ `--section` / `--source-type` / `--year`；`--move` 移动（默认复制）；`--force` 覆盖目标已有；`--dry-run` 只列清单不执行；迁移日志 `data/migrate_log.jsonl`；dense 索引迁移后需手动重建（BM25 自动感知） |
| `paper-search` | 多平台论文检索：arXiv/Crossref/OpenAlex/GS/万方/知网/**ePrint**/S2 → 去重 → 打分 → 入库(元数据) → 可选下载；每次搜索**持久化到 `data/paper_searches/`**（/papers 页可看历史、实时进度、删除记录）；**`--merge` 合并已存检索结果**（不搜索） | 搜索：`"关键词" [--rows N] [--no-gs] [--max-download N] [--inst-download N] [--inst-proxy auto\|socks5\|none] [--download-dir]`；**源选择**：`--only-sources crossref,eprint`（只用指定源）/ `--exclude-sources cnki,wanfang`（排除；源名=crossref/openalex/arxiv/google_scholar/wanfang/cnki/eprint）；**结构化条件**：`--title/--author/--year-from/--year-to/--category/--db-types`；`--max-download N`=最多成功 N 篇，下载按 id 命名并自动归档 `data/papers/{id}/paper.pdf`。**机构版下载 `--inst-download N`**：知网/万方/**IEEE/ACM** 全走 `browser/download_institution.js`；**IEEE/ACM 走 elib 反向代理，不需要校园网/wssocks/cf_clearance**（万方每篇约扣机构 3 元；`--inst-proxy` 只影响万方/知网/ACM兜底路由）。**合并模式**：`paper-search --merge [--searches id1,id2\|默认全部] [--keywords k1,k2] [--top-n N] [--min-citations M] [--export-json 路径] [--import] [--list-searches]`（全局去重：arxiv_id/DOI 主键 + 标题交叉兜底；关键词过滤标题；按引用降序取 top-N；`--import` 批量建 KB 元数据） |
| `download-papers` | 按 arxiv_id / DOI 清单**精确批量下载** PDF → `data/papers/{id}/paper.pdf`（并入 KB 二进制区） | `--arxiv 1712.01767,2504.21480 --doi 10.1109/tr.2021.3054617`；`--file 清单.txt`（每行一个 id，arXiv 正则自动识别）；`--import-meta` 顺带建 KB 元数据条目；arxiv 直连 arxiv.org、DOI 走 OpenAlex best_oa_location；**开放版拿不到时自动回落机构通道**（`elib.py` → IEEE/ACM 走 elib 反向代理，实测 24.8s 下到 3.06 MB `%PDF-1.4`；**默认直连**，走 wssocks 隧道会超时） |
| `project-import` | 代码项目入库：GitHub/GitLab/npm/Gitee/Bing 拉元数据 → `{调研分区}/projects/{id}/`（与论文同组，registry 有 `{分区}-projects` KB） | `<URL 或项目名> --kb 1-3-VERA [--host X] [--section 主题] [--tags 关键词] [--display-name 规范名] [--cn-note 中文一句话] [--force]`；**批量** `--file 清单.txt`；**渲染富字段回 md** `--render 分区 [--pid 项目]`（B1，BM25 可检索富字段）；**生态矩阵** `--matrix 分区`；**从论文提链接** `--scan 分区`；**候选清单** `--list-candidates 分区`；**schema 校验** `--validate 分区`；**超期清单** `--list-stale N`。host 自动识别（github 用 gh api 已登录 token；裸 owner/repo 精确查+自动重定向；纯名字 gh search repos→Bing）；`--display-name` 规范名；GitHub /languages 补全语言；license 空标 unknown；tags 分 manual_tags+auto 去重 |
| `paper-cookie` | 导出/刷新知网/万方登录 cookie（Playwright 手动登录，保存 storage_state） | `cnki\|wanfang` |
| `video-import` | 批量导入 B站视频：bvid/URL → 采集(bili.db) → 提取字幕+摘要 → 晋升 dense/video | `--bvids "BVxxx,BVyyy"` / `--file bvids文件`；`--no-extract` 只采集；`--timeout` |
| `condense` | 浓缩已转换论文 md → `knowledgebase/full/papers/{id}/condensation.md` + 晋升 `dense/paper/{id}.md` | `--survey <task_id>` / `--dir <md目录>`；`--model` |
| `paper-project` | **论文调研项目**（与 `project-import` 的开源项目调研完全分离）：init/status/edit/add/remove/move/run/list/import-pdf | 子动作：`init --name`；`status --project <slug>`；`edit --project <slug> --set key=value --set-paper-role id=role`；`add --project <slug> --papers id1,id2` 或 `--search-id <id>`；`remove/move`；`run --stage search|basic-matrix|semantic-matrix|classify|stats|refs`；工作区 `data/research_projects/{slug}/`，镜像 `knowledgebase/full/research_projects/{slug}/`（source_type=research_project，已注册 RAG KB） |
| `paper-matrix` | 文献矩阵：基础矩阵（无AI）与语义矩阵（AI，可选） | `--project <slug> --basic`（元信息+无AI打分）；`--semantic --model deepseek-chat`（依赖浓缩/摘要） |
| `paper-classify` | 文献角色分类：LLM 生成候选角色，人工确认后升级 | `--project <slug> --candidate`（LLM 候选）；`--status`；人工确认用 `paper-project edit --set-paper-role id=role` |
| `paper-refs` | 参考文献重要权威挖掘（OpenAlex；失败可本地频次兜底） | `--project <slug>`；产出 `candidate_references.json` + `reference_authorities.md` |
| `paper-stats` | 论文调研项目统计报告（Markdown + JSON，优先给 AI） | `--project <slug>` |

> 论文检索详情见 `论文检索/论文检索说明.md`；**RAG/知识库/检索系统设计（含加新内容类型的扩展指南）见 `知识库与检索系统设计.md`**（权威活文档）。

## 输出目录约定

| 路径 | 内容 |
|---|---|
| `output/survey/` `output/search/` `output/arxiv/` | arXiv 调研结果（JSON/CSV/报告） |
| `data/videos/{bvid}/` `data/videos/_analysis/` | 三遍法分析产物（该链已下线，存量档） |
| `data/bilibili/visual/{bvid}/` | 画面要素管线工作区：`digest.md`、`images_used/`（用上的截图）、`sections.json`、`lo2/`·`hd_frames/`·`final_frames/`（中间帧，默认跑完删） |
| `data/bili_subtitles/{bvid}.json` | 字幕缓存（`text` + 分段时间戳），画面管线与 `video-import` 共用 |
| `output/persona/` `output/frames/` | 人物画像 / 截帧缓存（非 B站分析报告） |
| `knowledgebase/` | **知识库（三层）**：`dense/`（浓缩/摘要，语义源）+ `full/papers\|docs\|web/`（全文，BM25） |
| `data/research_projects/{slug}/` | 论文调研项目工作区：project.json、留痕 jsonl、矩阵/统计/参考文献 JSON 等（不入 RAG） |
| `knowledgebase/full/research_projects/{slug}/` | 论文调研项目可检索镜像：project.md、matrix_meta.md、roles.md、stats.md 等（source_type=research_project） |
| `data/pdfs/` | 下载的 PDF（待处理区） |
| `data/remote_pdf/{job_id}/` | 远程转换结果（源 pdf + md + 图 tar，自包含） |
| `data/papers/{id}/` `data/videos/` | 论文二进制资产（paper.pdf/images）/ 视频处理记录 |
| `data/_kb_legacy/` | 论文收敛时移出的低优先级版本/冗余副本（不入 KB） |
| `data/bili_fav_*.csv` | 收藏夹导出 |
| `temp/` | 字幕、prompt 日志、远程作业状态 |

## 关键坑（先看再跑）

1. **入口是 `python -m backend.main`**，`python run.py` 已死。
2. `pdf`/`warmup`/`translate`/`remote-fs-*` 全走 SSH 远程服务器，依赖 `.env` 里 SSH 配置；连不上先说原因。
3. `ocr` 会**原地修改源 .md 文件**（图片下方插 OCR 结果），确认用户意图后再跑全量。
4. `chat --search` 官方 DeepSeek 平台不支持联网，仅部分第三方平台有。
5. 中文输出乱码/路径问题，参考 `windows-command-guide` skill 的说明书。
6. **Marker 远程转换的显存真相**：原 `convert.py` 是**单卡设计**（顶层 `CUDA_VISIBLE_DEVICES` 只暴露"空闲最多的一张卡"），且每 worker 实际占 **~7GB**（不是脚本注释说的 3.5-5GB）。所以**单卡 24GB 下 cap=3 就是上限，4 必 OOM**（4×7=28GB>24GB）。现在 `.env` 的 `REMOTE_CONVERT_SCRIPT` 指向双卡版 `convert_dual_gpu.py`：自动检测每卡剩余显存（`MARKER_WORKER_MB` 默认 7GB）分摊 worker，双 4090 最高 6 路；`--workers` 只是上限封顶。原 `convert.py` 保留在远程 `.bak_dual_*`。**别再手动把 convert.py 的 cap 调到 4 以上。**
7. **device 池必须交替轮询**（convert_dual_gpu.py 的坑）：多卡 worker 分配不能把同一卡连续排（如 `[0,0,0,1,1,1]`），否则任务数 ≤ 单卡容量时前 N 个全落一张卡、另一张空闲。要按容量**交替**（`[0,1,0,1,0,1]`）。
8. **本机 bge 嵌入是 3060 Laptop 30W 功耗墙**：本地 `ragsearch-index` 约 30 chunk/s 是**算力/功耗上限**，不是 batch 小（batch 16→128 + 全局聚合实测无提速）。真提速只能靠远程 4090（约 3-5×）——那需要把嵌入搬到远程，当前语料下可先不折腾。

### 坑: 重写启动脚本时 `.ps1` 丢 BOM / `.bat` 存成 UTF-8 → 假错与乱码

- 场景: 用 Edit/Write 或 `write_text(encoding='utf-8')` 重写根目录启动脚本（`server.ps1` / `start.bat` / `stop.bat`）
- 报错:
  - `.ps1` 无 BOM → PS 5.1 按 ANSI(GBK) 解析中文 → `Missing closing ')'` / `花括号不匹配` / `字符串缺少终止符`，**行号指向无关行，全是假错**
  - `.bat` 存 UTF-8 → cmd 按码页 936 读 → 中文乱码并吞相邻 ASCII；文件内 `chcp 65001` 救不了（读批处理时码页已定）
- 正确做法:
  - `.ps1` → **UTF-8 with BOM**：`Path(f).write_text(text, encoding='utf-8-sig')`
  - `.bat/.cmd` → **GBK/ANSI(936) + CRLF**：`Path(f).write_text(text, encoding='gbk', newline='
')`
  - `.sh` → **UTF-8 无 BOM**（BOM 破坏 shebang）
  - 校验：`read_bytes()` 查 `b[:3] == b'ï»¿'`，再试 `decode('utf-8')` / `decode('gbk')` 确认落到预期编码

### 坑: Git Bash 里后台起服务导致 `bash server.sh start` 卡住不返回，关窗口还会杀掉服务

- 场景: 在 Git Bash 里用 `Start-Process` 或 `&` 后台启动 uvicorn，命令迟迟不返回；关掉控制台后服务跟着停
- 原因: 后台进程继承 bash 的 stdout 管道 → 管道不关 → bash 等不到 EOF 不返回；控制台一关，进程被连带回收
- 错误尝试: `cmd //c start "t" /b powershell ...` 返回 rc=0 但服务没起来（stdout 仍是管道）
- 正确做法: 在 Python 侧用 Windows 真分离 API
  `subprocess.Popen(..., creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True, stdin=subprocess.DEVNULL, stdout=<日志文件>)`（非 Windows 用 `start_new_session=True`）
  实测：`bash server.sh start` 立即返回，杀掉调用方后服务仍存活（见 `run_api.py --launch`，因此不再需要 `server_start.cmd`）
- 附带: 子进程日志乱码要加 `PYTHONIOENCODING=utf-8`（Windows Python 默认按 GBK 写 stdout）

## 私域知识库框架：MCP server + Skill（M6，2026-10-15）

私域知识库框架已转正 **11 条**（2026-10-14/15，见领域 6）：`kb-scan / kb-inventory / kb-rebuild`（M1）、`kb-tags`（M2）、`kb-ask / kb-fetch / kb-eval`（M3）、`kb-embed`（M4 增量向量）、`kb-promote`（M5 证据双轨门槛）、**`kb-tag` / `kb-batch`（M6）**。**已无"尚未实现"的 kb-\* 命令**。

**MCP server**（六个 tool，供 agent 直接调用）：

```
D:/Anaconda/envs/infosystem/python.exe -m backend.services.kb.mcp_server      # cwd 必须是 D:\infosystem
```

server 名 `kb`；tool：`kb_ask` / `kb_inventory` / `kb_fetch` / `kb_tag` / `kb_promote` / `kb_batch_confirm`（参数各 6/2/3/3/2/5）。
- **本机 SDK 坑（M6 实测 / M8 已修）**：装的是 `mcp` **2.0.0**，高层类名是 `mcp.server.mcpserver.MCPServer`；**1.x 的 `mcp.server.fastmcp.FastMCP` 已被移除**。`backend/services/bilibili/subtitle.py` 的 `_get_mcp()` 曾按旧路径导入（`ModuleNotFoundError`）——**M8 已照 `mcp_server.py` 的 shim 修好**，三证：`import backend.main` 不炸、进程内拿到 `MCPServer`、真 stdio 子进程冷启 **1.22s** 并列出工具（`knowledgebase/_meta/reports/acceptance/m8_subtitle_check.json`）。`kb/mcp_server.py` 同样两代兼容。
- **常驻解决 deep 冷启**：bge 模型进程内单例惰性加载（首次 deep 才加载）。实测 MCP 冷启 **1.49s**；deep 首查 40–54s（含模型），**同进程二次 1.45–1.57s**；`kb_inventory` 12–38ms。CLI 每次冷启都重载模型（50–120s），**长驻场景一律走 MCP**。
- **错误语义**：handler 不抛异常，统一返回 `{"ok":false,"error":{"code","message"}}`（`unknown_doc` / `invalid_mode` / `missing_backlink` / `chunk_not_found` / `already_confirmed` / `empty_round` …）。
- **自测**：`$PY temp/m6_mcp_client.py --phase enumerate` 可列 6 tool；本机注册由评审者执行（**D39**：实施 AI 不改 `~/.codex`、`~/.claude`、`~/.dsh`）。建议 TOML 片段见 `knowledgebase/_meta/reports/acceptance/M6.log`。
- **Skill**：`C:\Users\HP\.claude\skills\私域知识库\SKILL.md`（Claude 系自动加载）；平台无关手册 `文档以及其它代码\(4)RAG与信息整合设计\私域知识库框架改造工作留档\06-使用手册.md`（六工具用法 + CLI 对照表 + 各平台注册片段，供 ChatGPT/DSH 等引用）。

进度与基线以 `文档以及其它代码/(4)RAG与信息整合设计/私域知识库框架改造工作留档/02-实现设计文档-交接版.md` §14 为准；M3 的 D33（browse 6/2/1/1）已采纳并在 M4 全量重建后复验 recall@10=0.8722、L0 8/8，原始日志见 `knowledgebase/_meta/reports/acceptance/M3.log` / `M4.log`。旧 BM25/FAISS 命令与 `ragsearch/` 全程未改，照常可用。

**M8 终态补充（2026-10-17）**：11 条 kb-* 命令**全部在位、无"尚未实现"**，本轮只做增强，不新增命令：

| 变化 | 说明 |
|---|---|
| `kb-ask` 增 `--toc-filter / --no-toc-filter`（默认开，M8） | 检索层剔除 PDF 转换出的"目录残渣块"（点线目录/编号短行目录），不参与折叠打分、不当证据块；全库判出 26 块/2 篇，零误报，recall 不降（02 §9.3.2） |
| `kb-ask` 的 evidence 增 `has_l3` / `quote_tier` / `quote_fallback`（M8） | "该引哪一档"不用猜：有 L3 时 `quote_tier=3`，否则回落到 `best_tier` 并标 `no_l3`/`meta_only`（02 §11.3） |
| `kb-fetch` 的 doc_id 歧义处理收窄（M8） | **精确命中的存储 ID 直接可用**（修掉 13 篇"双登记"论文 ask→fetch 断链）；同名前缀行作为 `alt_candidates` 知情项返回。无精确行时仍报 `ambiguous_doc_id`（02 §7） |
| `kb-eval` 增 `alt_accept_ids` 口径（M8） | 评测行可选"可接受替代命中"，报告单列 `alt_hit`，**不并入 recall@10 主指标**；另给 expect∪alt 口径 recall 作说明（02 §12.1）。当前：主指标 **0.8933**，expect∪alt **0.9083** |
| 折叠规则定案（M8，D36） | 50 条全量对照后**继续 `fold=max`**；`--fold top3sum` 仍是实验口（跨语言 +0.10 但 v1 −0.05、e029 1.00→0.00，02 §9.3.1） |

**回归与自愈**：改了内容/代码只跑四条命令（① `pytest backend/tests -q` ② `-m real` ③ `pytest ragsearch/tests -q` ④ `kb-eval --pretty`）；出问题先 `kb-embed --status --pretty` 看 `pending/stale`，兜底是 `kb-rebuild`（约 12–15 分钟全量重建）。详见 `06-使用手册.md §9`。

**何时用哪条**：要"我有什么"→ `kb-inventory --pretty`（体检）或 `kb-ask --mode browse`；要"具体细节、必须引原文"→ `kb-ask --mode deep` 拿 doc_id/tier/**chunk_id**，再 `kb-fetch <doc_id> --tier 3 --chunk-ids <id>` 取命中段落 ± 上下文（**不要**拿 L2 摘要当结论，除非 evidence 说 `must_quote_l3=false`；`quote_tier` 告诉你该引哪档）；中文提问想找英文论文 → 同样用 `kb-ask --mode deep`（向量支路，跨语言）；要晋升某篇到 L2 → `kb-promote <doc_id> --to-tier 2`（无回链会被拒）；给几篇打标签/删标签 → `kb-tag`；一批外搜结果要收进来 → `kb-batch open/add/confirm`；只在一批新需求导入后"这批进得对不对"→ `kb-eval`。**agent 场景优先用 MCP 六工具**（同一个进程反复用，省掉每次 50–120s 的模型冷启）。

## community backlog：备选调研方向（2026-10-17）

多来源手动清单，专记以后可学的项目/教程/工具；不按 QQ/贴吧/浏览器隔离。存储在 `knowledgebase/community/backlog/`（本地用户数据，不入库）。

```powershell
python -m backend.main backlog add "https://github.com/xxx/yyy"
python -m backend.main backlog add "标题 | https://example.com/repo | 以后抄它的调度逻辑" --source browser --tags 项目,Agent
python -m backend.main backlog import D:\notes\links.txt --source manual-import
python -m backend.main backlog list --tag 项目
python -m backend.main backlog status B20261017-XXXXXX learning --local-path E:\some\repo
```

实现：`backend/services/community/backlog.py`；同一 URL 默认拒绝，`--update` 合并备注/标签；`import` 支持 txt/md/html/json 与 stdin。

## tieba：贴吧抓取与知识点提炼（2026-10-17）

正式 CLI 已接入，实现位于 `backend/services/community/tieba/`。默认只保存原子知识点、被引用证据和行级 hash；不保存全帖流水、无关楼层、截图或 OCR。

```powershell
python -m backend.main tieba cookie-check
python -m backend.main tieba login --bduss <BDUSS>
python -m backend.main tieba thread <TID>
python -m backend.main tieba search <吧名> <关键词> --max-pages 3
python -m backend.main tieba favorites --urls
python -m backend.main tieba extract <TID>
python -m backend.main tieba show <TID>
```

> **只读看帖 ≠ 入知识库（2026-10-20 实测）**：`thread` 是纯读路径——抓全帖（含楼中楼）打成 JSON 打到 stdout，不写任何产物、不动 kb（`--help` 原文「只输出，不作为长期档案保存」）；要沉淀知识点才用 `extract`。用户说"看看帖子里有没有 X"时**只用 `thread`**。
>
> **不要用浏览器 / curl 硬爬**：直连 `https://tieba.baidu.com/p/<tid>` 未登录会拿到 403「百度安全验证」（`web_fetch`、`Invoke-WebRequest` 同样 403）。`tieba` 走贴吧客户端/网页 API + BDUSS Cookie，先 `cookie-check` 确认登录态（应返回 `logged_in: true` + 用户名）。
>
> **规模与编码**：TID 11117873910 实测 9 页 240 楼带楼中楼、一次抓完约 1 分钟、stdout ≈ 893 KB JSON。重定向到文件时 PowerShell `>` 默认写 **UTF-16**，Python 解析要按 `utf-16` 读（先看前两字节 BOM），否则 `json.loads` 报 `line 1 column 2`。

`extract` 会先粗筛灌水，再让 LLM 抽原子知识点；重复运行无变化时不调用模型。`00-备选调研方向` 里带直链的项目会同步进全局 backlog。

> **extract 的三个产物（2026-10-20 起）**：① `data/community/tieba/knowledge/thread_<tid>.json`（知识点 + 证据）；② `knowledgebase/community/tieba/knowledge/tieba-thread-<tid>.md`（自动渲染，就是 `kb-scan --source external` 的真相源）；③ 主题 `00-备选调研方向` 且带直链的条目**自动进跨来源候选库** `backlog`。入库闭环：`tieba extract <tid>` → `D:/Anaconda/envs/infosystem/python.exe -m backend.main kb-scan --source external` → `kb-embed --topic ai-agent` → `kb-ask`（kb-scan 需要 sqlite-vec，必须用 conda `infosystem` 环境跑）。
>
> **embed 两坑（2026-11-01 实测，TID 11145874046）**：① 贴吧知识点固定落 **topic `ai-agent`**、kind `tieba_distill`（tier 3），`kb-embed --status` 不带 topic 也看得到，但——② **`kb-embed --status` 对刚入库、还没出块的新版本会误报 `pending=0`**（当时该 version chunks=0 仍报 0/0）。别拿 status 当"要不要 embed"的依据，scan 完**直接跑 `kb-embed --topic ai-agent`**，看返回 JSON 里 `updated≥1, embedded>0` 才算闭环；嵌完再 `--status` 验 `pending=0` 才有意义。
>
> **自动收录的边界**：直链只认「同一楼且能对上条目对象名」的 GitHub/Gitee/GitLab/HF 链接，对不上就留空（防止一栋楼里多项目串台）；帖子里只提名字没给链接的项目不会被自动收，用 `backlog add "名称 | URL | 备注" --source tieba --source-ref <帖URL>` 手工补。

Web API 前缀：`/api/community/tieba`；知识点写入口是 `POST /knowledge/{tid}/extract`，其余为抓取/读取。

## qq：QCE 导出与归档管线封装（2026-10-17）

正式封装在 `backend/services/community/qq/`。流水线程序已迁入 infosystem；NapCat/QCE 应用本体仍在 `D:\QQChatExporter`。infosystem 不复制登录态，也不直接写 `消息.tsv`。

```powershell
python -m backend.main qq export-status
python -m backend.main qq export-dry-run
python -m backend.main qq archive-list
python -m backend.main qq plan "AI Agent 综合交流"
python -m backend.main qq kb-check "AI Agent 综合交流"
python -m backend.main qq verify
```

写操作必须双层确认，且默认 dry-run：

```powershell
python -m backend.main qq export --confirm --launch
python -m backend.main qq prune-missing <群名> --execute --confirm
python -m backend.main qq analyze-run <群名> --execute --confirm --limit 1
python -m backend.main qq kb-add <群名> --execute --confirm
```

只读 Web API 前缀：`/api/community/qq`；不提供同步导出/LLM 花钱端点。集成验收可直接跑 `qq verify`。

## 补记：单文件 PDF→MD（远程 Marker）完整跑法（2026-10-17 实测）

场景：任意位置（桌面 / `D:\Research\...`）的**单个** PDF 要转 md，不想先搬进 `data/pdfs/`。

```powershell
cd D:\infosystem
python -m backend.main pdf --files "D:\某个目录\paper.pdf"   # ① 提交
python -m backend.main warmup                                # ③ 等一阵后拉结果
```

要点与坑：

1. **`pdf --files` 直接吃绝对路径**：`submit_batch_task` 只校验 `os.path.isfile` + `.pdf` 后缀，不必先复制进 `data/pdfs/`。回执只代表"远端已启动"，给 `Job ID / 远端 PID / 远端日志路径`。
2. **同名去重只是提示，不是拒绝**：stderr 出现 `文件 ['...'] 已在已完成作业 ['xxx'] 中出现过，将重新提交` 时照常建新 job。真正的内容级去重按 **SHA256**，命中才会 `All files already have existing jobs` 直接跳过。
3. **命令退出码 1 常常是假错**：PowerShell 把 stderr 的任何输出包成 `NativeCommandError`。成功与否看回执里有没有 `Job ID`。
4. **`warmup` 是全局收尾**，会把所有进行中 job 一起收，不是只收你刚提的那个。输出里按 job id 认领；作业台账在 `temp/remote_jobs.json`。
5. **产物结构**：`data/remote_pdf/{job_id}/` = `paper.md`（正文）+ `paper.tar`（图）+ 源 PDF 副本。tar 内部带一层 `<stem>/` 目录（如 `paper/_page_4_Figure_3.jpeg`），解包要 **strip 顶层目录**，否则 md 里的相对图片引用会断。
6. **交付习惯**：把 `paper.md` 复制到源 PDF 同目录 + 把图解到同目录，md 才能就地预览。
7. **PowerShell 里 `鈮?` / `鈭?` 不一定是文件坏**：PS 控制台按 GBK 解码 UTF-8 的 `≥` / `−` 就会长这样。核实用 `$env:PYTHONIOENCODING='utf-8'; python -c "open(p,encoding='utf-8').read()"`——实测文件本体是干净 UTF-8。
8. **远端时钟可能与本机差几天**（回执里的远端日志名 `convert_20261012_102937.log` vs 本机 10-17），别拿它当时间戳判断新旧，看 `data/remote_pdf/` 目录的本地 mtime。
9. 速度参考：28 页 PDF 双卡 6 worker，提交到 `warmup` 收货约 **90 秒**。

## 补记：单篇本地 PDF 走完整入库（2026-10-29 实测）

场景：Downloads 里一篇 PDF（ACM TOSEM，55 页）要进库并可被 `kb-ask` 检索。四步，顺序不能乱：

```powershell
python -m backend.main pdf --files "C:\Users\HP\Downloads\3815587.pdf"   # ① 远程 Marker 转换（约 4 分钟/55 页）
python -m backend.main warmup                                            # ② 收货到 data/remote_pdf/{job_id}/
python -m backend.main add-paper --doi 10.1145/3815587                   # ③ 建题录（Crossref/OpenAlex）
# ④ 手工补三处：paper.md（带 frontmatter）+ papers_meta 扁平题录 + data/papers/{id}/paper.pdf
$PY -m backend.main kb-scan --source all                                 # ⑤ 约 250s
$PY -m backend.main kb-embed --topic <分区>                               # ⑥ 55–130s，看 --status 应 pending=0 stale=0
```

四个必知坑：

1. **`add-paper` 写的 `papers/{id}/metadata.json` 不是 kb-scan 的输入**。`_scan_metadata` 只 glob `papers_meta/*.json`（扁平，2026-10-20 拆空的产物）。必须把同一份 json **复制**到 `<分区>/papers_meta/{id}.json`，否则该文只有 L1、没有 L0 题录（abstract/tags/venue 全空，`--topic` 体检里看不见）。
2. **换分区不能只 mv 目录**：只要旧的 doc 行还在库里，`_global_doc_id` 会给新分区生成 `cbac:{id}` 这种带前缀的 doc_id（设计文档明令禁止的"两条互不相认的记录"）。正确做法：把 `documents / versions / chunks / kb_fts / kb_vec` 里那条旧记录一起删掉（注意 sqlite-vec 要用 `backend.services.kb.db.connect()` 才加载得到 `kb_vec`，裸 `sqlite3.connect` 会报 `no such module: vec0`），再跑 `kb-scan`。搬家后必须 `kb-embed --topic 新分区` 重新出块。
3. **frontmatter 写 `org_marks: [实验室内]`** 才有 `--org 实验室内` 过滤；tags 用方括号数组，多词标签要连字符（`parse_tags` 会把空格也当分隔符，"smart contracts" 会被拆成两个词）。
4. **图不用丢弃**：`warmup` 收的 `{stem}.tar` 内带一层 `<stem>/`，解包 strip 掉一层、把 `_page_*_Figure_*.jpeg` 放到 paper.md 同目录，md 里的相对引用就能直接渲染（`knowledgebase/full/web/runoob/images/` 已有先例，小图不违反"二进制不进 knowledgebase"）。

## 补记协议

遇到本文件未覆盖的新坑并解决后，立即：
1. 按领域追加到对应表格（新增命令也要登记）。
2. 若 `--help` 显示的参数与本文件冲突，以 `--help` 为准并同步更正本文件。
