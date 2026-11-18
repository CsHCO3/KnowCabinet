# InfoSystem 前端 — API 设计文档

> 版本：0.1.0
> 适用代码范围：`api/`（FastAPI 后端）+ `frontend/`（服务端渲染模板）
> 最后更新：2026-08-08

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────┐
│  浏览器                                                       │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  Jinja2 模板（服务端渲染 HTML）                        │  │
│  │  + HTMX（局部刷新，hx-get/hx-post/hx-target/hx-swap）│  │
│  │  + 内联 <script>（fetch 调用 JSON API）              │  │
│  │  + Bootstrap 5 + Bootstrap Icons                      │  │
│  └─────────────────────────────────────────────────────┘  │
└───────────────────────────┬──────────────────────────────┘
                            │ HTTP (JSON / HTML partial)
┌───────────────────────────┴──────────────────────────────┐
│  FastAPI (api/main.py)                                     │
│  ├─ 页面路由：返回完整 HTML 页面                          │
│  ├─ API 路由（前缀 /api/*）：返回 JSON 或 HTML partial     │
│  ├─ Jinja2Templates（templates 注入到 app.state）         │
│  └─ SQLite（api/services/bili_db.py）                     │
└───────────────────────────────────────────────────────────┘
```

### 关键技术选择
- **服务端渲染（SSR）+ HTMX**：页面首次由后端渲染完整 HTML，后续交互通过 HTMX 只换局部片段（`hx-swap="outerHTML"`），无需前端框架。
- **双形态响应**：同一端点通过 `hx_request: bool = Header(False)` 判断请求是否来自 HTMX：
  - 来自 HTMX（带 `HX-Request` 头）→ 返回 `partials/*.html` 片段；
  - 来自 `fetch` 或浏览器直接访问 → 返回 JSON。
- **统一响应壳**：业务接口统一返回 `APIResponse { success, message, data, error }`。
- **后台任务**：耗时操作（字幕提取、问答）通过 `BiliTaskManager`（线程池）异步执行，前端轮询状态。

---

## 2. 页面路由（完整 HTML 页面）

所有页面路由定义在 `api/main.py`，统一返回 `HTMLResponse`。

| 路径 | 模板 | 说明 |
|------|------|------|
| `/` | `dashboard.html` | 仪表盘首页 |
| `/bilibili/collect` | `bilibili/collect.html` | **页面1：采集**（URL/作者/收藏夹 → 入队 → 入库） |
| `/bilibili/extract` | `bilibili/extract.html` | **页面2：提取**（未提取视频 → 批量字幕+总结） |
| `/bilibili/query` | `bilibili/query.html` | **页面3：问答**（已提取视频 → 多轮问答） |
| `/survey` `/survey/start` | `survey/start.html` | arXiv 调研（传统） |
| `/survey/smart` | `survey/smart.html` | arXiv 调研（智能搜索） |
| `/survey/quick-test` | `survey/quick.html` | arXiv 调研（快速测试） |
| `/tasks` | `tasks.html` | 后台任务列表 |
| `/tasks/submit` | `tasks_submit.html` | 提交远程转换（上传/选已有 + 引擎 + 选项） |
| `/task-center` | `task_center.html` | 统一任务中心（全部类型/筛选/重试/取消） |
| `/ocr` | `ocr.html` | OCR 识别（选 md + 模型 → 后台 → 逐图结果） |
| `/archive` | `archive.html` | 归档管理（论文手风琴：五阶段状态 + 一键转换/OCR/浓缩） |
| `/videos` | `videos.html` | **视频管理**（列表/筛选/提取字幕+摘要/批量删除；摘要晋升 dense/video） |
| `/rag` | `rag.html` | **RAG 问答**（选库/参数/问题 → 检索 + LLM 回答 → 留痕 history） |
| `/models` | `models.html` | 模型配置与连通性 |
| `/prompts` | `prompts.html` | 提示词库（目录树 + 内容预览） |
| `/settings/social` | `settings/social.html` | 社交平台（B站）设置 |
| `/about` | `about.html` | 关于 |

---

## 3. B站模块 API（`/api/bilibili`）

这是系统的核心模块，分三个页面：**采集 → 提取 → 问答**，数据流单向推进：

```
采集(入库) ──▶ 提取(字幕+总结) ──▶ 问答(基于 summary_text)
```

### 3.1 页面1：采集（Collect）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| POST | `/collect/check-url` | `Form: url` | `CheckUrlResult` | 检查视频 URL，解析视频信息+合集信息，返回是否已在库 |
| POST | `/collect/search-author` | `Form: mid`(数字或完整space URL), `count` | `AuthorSearchResult` | 搜索作者视频（URL 经 `extract_mid` 解析） |
| GET | `/collect/favorites` | — | 收藏夹列表 | 获取 B站收藏夹列表 |
| POST | `/collect/favorites/{fav_id}/preview` | `Form: count` | `FavoritesPreviewResult` | 收藏夹内视频预览（**不含作者/播放/时长**，需补全） |
| POST | `/collect/commit` | `CommitRequest{videos[]}` | `APIResponse{data:{inserted,skipped}}` | 批量提交视频（按 bvid 去重，`INSERT OR IGNORE`） |
| POST | `/collect/enrich-batch` | `bvids[]`(JSON body) | `{videos[]}` | 批量调 view API 补全作者/播放/时长（收藏夹队列用） |
| POST | `/videos/enrich-existing` | `bvids[]` | `{updated,failed}` | 已入库视频批量补全数据 |
| POST | `/videos/reset-stuck` | — | `{reset,message}` | 将 `subtitle_status='extracting'` 的视频重置为 `none` |

**前端交互（`collect.html` 全部用 fetch）：**
- 粘贴 URL → `check-url` → 显示视频卡片 →「加入队列」
- 作者 MID/URL → `search-author` → 列表 → 勾选 →「加入选中到队列」
- 收藏夹 → `favorites` → 选文件夹 → `favorites/{id}/preview` → 勾选 → `enrich-batch`（补全）→「加入选中到队列」
- 队列（内存数组）→「全部提交入库」→ `commit`

### 3.2 页面2：提取（Extract）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/extract/list` | `keyword,author,view_min,view_max`(+`HX-Request`) | `{videos, total, filters}` | 列出 `subtitle_status='none'` 的视频（**2026-08 起不再带任务列表，任务统一走 `/extract/tasks`**） |
| POST | `/extract/submit` | `ExtractSubmitRequest{bvids[]}` | `{task_id}` | 提交批量提取+总结任务 |
| GET | `/extract/task/{task_id}/status` | task_id | `ExtractStatusResult` | 查询任务状态/进度/逐条结果 |
| GET | `/extract/task/{task_id}/content` | task_id | `APIResponse{data:{task_id, videos:[{bvid,title,author,summary_text}]}}` | 获取任务中已 `summarized` 视频的总结内容 |
| GET | `/extract/tasks` | `+HX-Request` | `{tasks[]}`（pending/running 在前 + 最近10个已完成） | 所有提取任务统一列表，供任务面板轮询 |
| POST | `/extract/task/{task_id}/cancel` | task_id | `APIResponse` | 取消任务 |

**提取流程（后台线程 `_run_extract`）：**
1. 对每个 bvid：`get_video_by_bvid` → 设 `subtitle_status='extracting'`
2. `get_bilibili_timestamped_subtitle(url)` 提取字幕（**包 try/except**，失败则置 `failed` 不卡死）
3. 字幕为空 → 置 `extracted`+`failed`，跳过总结
4. 调 LLM（模型 `v4flash`）生成 `summary_text`，置 `summary_status='summarized'`
5. 异常 → `summary_status='failed'` 且前端可见

**前端交互：**
- 列表用 HTMX 表单提交（`hx-get /api/bilibili/extract/list`）刷新
- 勾选 →「提取选中」→ `extract/submit` → 轮询 `/extract/tasks`（统一任务列表）
- 任务完成显示于「最近完成」，可「详情」查看逐条状态（`extract/task/{id}/content` 取已总结视频的内容）
- 提供「补全数据」「重置卡住」按钮

### 3.3 页面3：问答（Query）

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/query/videos` | `keyword,author,view_min,view_max`(+`HX-Request`) | `videos[]` + `has_summary` | 列出 `subtitle_status='extracted'` 的视频（**不论总结状态**） |
| POST | `/query/run` | `QueryRunRequest{video_ids[],prompt,prompt_file_path,filter_mode,keyword}` | `{task_id}` | 创建并运行问答任务 |
| GET | `/query/tasks` | — | `{tasks[]}` | 历史问答任务列表（`deleted` 状态被过滤） |
| GET | `/query/tasks/{task_id}` | task_id | `APIResponse{data}` | 任务详情（`result_text` 自动解包为 `{text, used_videos}`） |
| DELETE | `/query/tasks/{task_id}` | task_id | `APIResponse` | 删除任务（置 `status='deleted'`） |

**问答流程（`_run_query`）：**
1. 取 `video_ids` 对应视频，过滤出有 `summary_text` 的
2. `filter_mode='keyword'`：按 `keyword` 在 `summary_text` 中 LIKE 过滤，仅用命中的视频
3. `filter_mode='keyword_llm'`：命中视频在 prompt 中标记 `★` 优先，但保留全部供 LLM 自选
4. 组装 system+user 消息 → 调 LLM → 返回 `result_text`
5. 返回体中额外携带 `used_videos`（实际进入 LLM 上下文的视频清单）

**前端交互（`query.html`）：**
- 布局自上而下：① 问答设置 | 任务记录 ② 问答结果（全宽） ③ 已提取视频（全宽，含删除按钮）
- 视频列表用 `hx-get ... hx-trigger="load"` 自动加载
- 选择视频 + 输入问题 →「开始问答」→ 轮询 `query/tasks/{id}`
- 结果区显示：实际使用的视频徽章 + 完整答案
- 历史任务可「查看」（含 `used_videos`）/「删除」

### 3.4 通用视频管理

| 方法 | 路径 | 入参 | 返回 | 说明 |
|------|------|------|------|------|
| GET | `/videos` | `keyword,author,source_type,view_min,view_max,subtitle_status,summary_status,limit,offset` | `{videos[],total}` | 全量视频列表（组合筛选，`author` 用 LIKE） |
| DELETE | `/videos/{video_id}` | `cleanup_files`(query) | `DeleteResult` | 删除单条视频（可选清理字幕文件） |
| POST | `/videos/batch-delete` | `BatchDeleteRequest{video_ids[],cleanup_files}` | `APIResponse` | 批量删除 |

---

## 4. 其他 API 模块

### 4.1 模型（`/api/models`）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/models` | 模型列表（从 `model_config.json` 读取，检测端点连通性） |
| GET | `/models/{key}` | 单个模型详情 |

### 4.2 提示词库（`/api/prompts`）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/prompts` | 提示词目录树（`hx_request` 返回片段） |
| GET | `/prompts/{path:path}` | 单个提示词文件内容 |

> 每个文件用途记录在外部 `backend/pipelines/prompts/prompts_meta.json`，列表右侧显示用途说明（不写入提示词文件本身，避免污染 LLM 输入）。

### 4.3 社交平台设置（前缀 `/api/settings/social`）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/bilibili` | B站绑定状态 |
| POST | `/bilibili/test` | 测试配置 |
| PUT | `/bilibili` | 更新配置 |
| DELETE | `/bilibili` | 清除配置 |

### 4.4 后台任务（`/api/tasks`）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks` | 任务列表（HTMX 片段） |
| DELETE | `/tasks/{task_id}` | 删除任务 |

### 4.5 arXiv 调研（`/api/survey`, `/api/arxiv`）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/surveys` | 调研任务列表 |
| POST | `/surveys` | 创建调研任务（含 arXiv 搜索/分析子流程） |
| GET | `/surveys/{task_id}` | 调研详情 |
| GET | `/surveys/{task_id}/progress` | 调研进度 |
| POST | `/surveys/{task_id}/cancel` | 取消调研 |
| POST | `/arxiv/parse-query` | 解析查询 |
| POST | `/arxiv/parse-raw` | 解析原始查询 |
| POST | `/arxiv/search` | arXiv 搜索 |
| POST | `/arxiv/analyze` | arXiv 分析 |

### 4.6 远程转换任务（`/api/pdf/*`，2026-08-08 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/pdf/engines` | 两引擎能力 + 选项（marker 只收 pdf；mineru 收 pdf/pptx/docx/xlsx/图片），前端渲染表单 |
| POST | `/pdf/submit` | 提交远程转换（multipart）：`engine` / `source`(upload\|existing) / `files[]` / `paths`（逗号分隔）/ marker 的 `translate` / mineru 的 `method lang backend formula table`；上传存 `data/remote_inbox/`，**失败自动清理**；提交前先跑远端 Ollama Forever 清理 + GPU 显存预检（不足直接报错） |
| GET | `/pdf/local-files` | 列本地支持格式文件（默认 `data/pdfs`，`?dir=` 可指定） |
| GET | `/pdf/jobs/{job_id}` | 任务详情（本地记录字段，快） |
| GET | `/pdf/jobs/{job_id}/log` | 远端日志 tail（独立 SSH，异步加载，不阻塞详情） |
| GET | `/pdf/gpu-status` | 远端 GPU 显存状态（只读；`ok_for_submit` 表示是否达到提交阈值） |

> **实现**：`api/routers/pdf_tasks.py`，复用 `backend.services.pdf_remote`（`RemotePDFService`），与 CLI `pdf`/`mineru` **完全同链路**（同一 remote_jobs.json、同一 SSH 提交、同一去重）。**文件生命周期**：提交成功即把源文件**复制**进 `data/remote_pdf/{job_id}/`（自包含），`data/pdfs` 原件保留。
> **前端页面**：`/tasks`（列表/删除/👁 详情，每 5s 自动刷新）、`/tasks/submit`（提交：上传/选已有双 Tab + 引擎下拉 + 选项面板 + GPU 状态提示；用户手动选过引擎后不再按扩展名自动跳回）。
> **日志拆分原因**：详情接口含 SSH 拉日志会卡十几秒，已拆成 `/jobs/{id}/log` 独立接口，前端先渲染详情再异步补日志。

### 4.7 RAG 问答（`/api/rag/*`，2026-08-11 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/rag/kbs` | 知识库列表（名称/文档数/路径/index_ready） |
| POST | `/rag/query` | 提交问答（multipart）：`question` / `kb_name` / `top_k` / `model_key` / `section tags year authors source_type` / `group_by_doc` / `system_prompt` / **`retrieval=bm25\|dense`**（默认 bm25；dense = bge-m3 段落语义检索，须先 `ragsearch-index`，`/rag` 页有检索模式下拉） → 后台线程执行 → 返回 `query_id` |
| GET | `/rag/tasks` | 最近问答记录（HTMX 请求返回 partial `rag_tasks.html`） |
| GET | `/rag/tasks/{qid}` | 单条记录（前端轮询：status/answer/retrieved_docs） |
| DELETE | `/rag/tasks/{qid}` | 删除记录 |
| POST | `/rag/reindex` | 重建某库 BM25 索引（`kb_name`） |
| POST | `/rag/search-only` | 只检索不调 LLM（排查”文件有没有进库”）；支持 `retrieval=bm25\|dense` + `tags`/`section`/`source_type` 过滤 |

> **库间迁移（`rag-migrate`）暂为 CLI-only**（`python -m backend.main rag-migrate --from A --to B ...`），无 Web 端点；用法见 `知识库与检索系统设计.md` 路 C 与 `infosystem命令速查.md`。

> **实现**：`api/services/rag_store.py`（SQLite `data/rag.db` 的 `rag_queries`，留痕）+ `rag_service.py`（后台线程：`search_kb`/`search_all` 检索 → **先落检索结果** → `Settings().get_llm_client()` 回答 → 更新记录）。检索复用 `backend.services.ragsearch.service`（BM25 引擎，不调 ragsearch HTTP）。0 命中时直接返回“知识库未检索到相关内容”并提示放置目录，不再把空上下文丢给 LLM。
> **前端页面**：`/rag`（选库 + 参数 + 问题 → 轮询出回答+检索片段 → 右侧历史查看/删除；另有「仅检索」「重建索引」排查按钮）。

### 4.8 视频管理（`/api/bilibili/videos*`，2026-08-11 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/bilibili/videos` | 视频管理列表（全状态，筛选：keyword/author/subtitle_status/summary_status；HTMX 返回 `videos_table.html`） |
| GET | `/bilibili/videos/overview` | 状态概览（总数 + 字幕/摘要各状态计数） |
| POST | `/bilibili/videos/extract` | 选中视频 → 后台 `extract_and_summarize`（提取字幕+摘要，**摘要自动晋升 dense/video**） |
| POST | `/bilibili/videos/batch-delete` | 批量删除（含字幕文件） |
| GET | `/bilibili/videos/extract/status/{task_id}` | 提取任务状态（前端轮询） |

> **实现**：`api/routers/bilibili.py`，复用 `BilibiliService` + `bili_db`。摘要生成后 `promote_to_dense("video", ...)` 晋升 L1 知识库（RAG 可搜）。
> **前端页面**：`/videos`（概览徽章 + 筛选 + 勾选表格 + 提取/删除 + 轮询）。
> **批量导入**：CLI `video-import --bvids "BVxxx"`（采集→提取+摘要→dense/video）。

### 4.9 归档管理（`/api/archive/*`）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/archive/overview` | 论文/视频 + 五阶段状态（pdf/md/ocr/translate/condense）+ 缺失列表 |
| POST | `/archive/run` | 重跑归档（拾取新文件、解压 tar、刷新 frontmatter/metadata/status） |
| POST | `/archive/papers/{id}/ocr\|condense\|convert` | 一键补做缺失阶段（后台统一任务） |

> **实现**：`api/routers/archive.py`，复用 `backend.services.archive`（归档到 `knowledgebase/full/papers/`，二进制到 `data/papers/{id}/`）。

### 4.10 多平台论文检索（`/api/papers/*`，2026-08-12 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/papers/searches` | 搜索任务历史（列表：query/时间/每源命中/总数/下载数/status/stage），读 `data/paper_searches/_index.json` |
| GET | `/papers/searches/{id}` | 单次任务详情（每源命中/结果打分/下载源分类/下载文件/下载尝试与失败原因） |
| POST | `/papers/search` | 触发多平台搜索（multipart：`query rows no_gs max_download inst_download title author year_from year_to category only_sources exclude_sources` → **先写 running 占位记录** → 后台跑 CLI `paper-search --search-id`，写 `data/paper_searches/`）。`only_sources`/`exclude_sources`=逗号分隔源白名单/排除（crossref/openalex/arxiv/google_scholar/wanfang/cnki/eprint），空则不限制 |
| DELETE | `/papers/searches/{id}` | 删除搜索记录（只删记录，已下载 PDF 保留） |

> **实现**：`api/routers/papers.py`，后台 subprocess 跑 `backend.main paper-search`（复用 CLI 全链路），stdout 写 `data/paper_searches/{id}.log`；CLI 每阶段更新 `status/stage`。
> **前端页面**：`/papers`（导航「论文调研→多平台检索」）：搜索表单（含**高级过滤** title/author/year/category，映射 API 三平台高级语法）+ 任务历史（实时状态徽章，每 5s 自动刷新 running 任务；展开看每平台结果/打分/下载；每行可删除记录）。GS 默认不勾选（慢）。
> **结构化查询**：`backend/services/papers/query.py` 的 `PaperQuery` + 每源 builder（arXiv `all:/ti:/au:/cat:/submittedDate`、Crossref `query.bibliographic/title/author + filter 日期`、OpenAlex `search + filter title.search/author.search/日期/topics.search`）。GS/万方/知网 仍用关键词。
> **机构版下载（2026-09 扩展，CLI-only）**：`--inst-download` 除知网/万方外新增 **IEEE/ACM（elib 反向代理，无需校园网/VPN/爬虫 Cookie）**；新增 `--inst-proxy auto|socks5|none` 路由参数——**API 侧未暴露 inst_proxy**（`/api/papers/search` 仅透传 `inst_download`），需要用代理路由走 CLI。实现：`backend/services/papers/elib.py` + `browser/elib_token.js` + `browser/download_institution.js`；机制与风险见 paper-sources skill 与 `(5)搜集信息源\论文检索\elib校外访问代理自动化.md`。

### 4.11 kb 目录层（私域知识库框架，2026-10）——**无 Web 端点（有意为之）**

> kb-* 命令族（kb-scan / kb-inventory / kb-ask / kb-fetch / kb-tags / kb-embed / kb-eval / kb-rebuild / kb-promote / kb-tag / kb-batch）**只有 CLI 和 MCP 两种入口，不进 `api/`**：主用法是 Agent 直连（MCP server `backend/services/kb/mcp_server.py`，6 tools；Codex 已注册），Web 页面属 M8 后可选扩展。设计见 `(4)RAG与信息整合设计\私域知识库框架改造工作留档\02-实现设计文档-交接版.md`；接口契约见 `(4)RAG与信息整合设计\知识库接口契约.md` §6。旧 `/api/rag/*`（§4.7）仍是旧 BM25/dense 链路的 Web 入口，两者并存不冲突。

---

## 5. 数据模型（SQLite）

### 5.1 `videos` 表
| 字段 | 说明 |
|------|------|
| `id` | 主键 |
| `bvid` | 视频 BV 号（唯一约束，去重依据） |
| `title, author, author_mid` | 标题 / 作者 / 作者 MID |
| `duration, pubdate, view` | 时长(秒) / 发布时间 / 播放量 |
| `pic, description` | 封面 / 描述 |
| `source_type, source_detail` | 来源类型（`manual`/`favorites`/`author`）/ 来源详情 |
| `subtitle_status` | `none` → `extracting` → `extracted` / `failed` |
| `summary_status` | `none` → `summarized` / `failed` |
| `subtitle_path` | 字幕 JSON 文件路径 |
| `summary_text` | LLM 生成的总结文本 |
| `created_at` | 入库时间 |

### 5.2 `query_tasks` 表
| 字段 | 说明 |
|------|------|
| `id` | 主键 |
| `title` | 任务标题 |
| `user_prompt` | 用户问题 |
| `filter_mode, keyword` | 筛选模式 / 关键词 |
| `video_ids` | 选中的视频 ID 列表（JSON） |
| `result_text` | 结果（JSON：`{text, used_videos}`，前端解包显示） |
| `status` | `pending/running/completed/failed/deleted` |
| `created_at, completed_at` | 时间戳 |

---

## 6. 前端约定

### 6.1 模板布局
- `base.html`：HTML 骨架 + `<head>` 资源（Bootstrap 5 CDN、Bootstrap Icons、HTMX CDN）
- `nav.html`：顶部导航栏（`base.html` 中 `{% include %}`）
- 各页面模板 `{% extends "base.html" %}`，通过 `{% block content %}` / `{% block scripts %}` 填充

### 6.2 交互模式
- **HTMX 局部刷新**：表单 `hx-get` + `hx-target="#xxx"` + `hx-swap="outerHTML"`；后端检测 `HX-Request` 头返回 `partials/*.html`
- **fetch + JSON**：按钮点击用 `fetch('/api/...')` 调 JSON 接口，JS 处理响应（如 `extract/submit`、`query/run`）
- **轮询**：前端 `setTimeout` 轮询 `*/status` 或 `*/tasks/{id}` 接口获取进度
- **模态框**：Bootstrap Modal 用于查看完整摘要（`summary-modal`）、提示词内容等

### 6.3 通用 JS 函数（各页内联）
- `updateQueryBtn()` / `updateExtractBtn()`：根据勾选状态更新按钮可用性与计数
- `showSummary(id)`：弹出摘要模态框
- `runQuery()` / `pollQueryResult()` / `loadHistory()`：问答流程
- `refreshList()`：用 `htmx.ajax('GET', ..., {target, swap, values})` 重新加载列表

---

## 7. 关键设计决策与坑点

1. **`subtitle_status='extracting'` 卡死问题**：字幕 API 调用必须包 `try/except`，否则异常会让视频永久卡在 `extracting`，既不在提取页也不在问答页。已通过「重置卡住」接口兜底。
2. **问答页初始数据**：页面首次渲染必须 `hx-trigger="load"` 主动拉取，否则 `include` 的 partial 因缺少 `videos` 参数而显示空白。
3. **作者筛选用 LIKE**：`author` 字段在 `bili_db` 中改为 `LIKE '%x%'`，支持模糊匹配。
4. **API Key 不在代码中**：`model_config.json` 的 `api_key` 用 `ENV:VAR_NAME` 引用，真实密钥存于 `.env`（已加入 `.gitignore`，并从 git 取消追踪）。`settings.py` 启动时 `load_dotenv()` 注入 `os.environ`。
5. **LLM 调用的 401 根因**：pydantic-settings 的 `env_file` 只填充 Settings 字段、不导出到 `os.environ`；`_resolve_api_key` 读 `os.environ` 导致线上解析为空。修复为显式 `load_dotenv()`。
6. **提示词与用途分离**：提示词文件保持纯净（不被 LLM 读到注释），用途说明集中在 `prompts_meta.json`，前端列表右侧展示。

### 7. 环境与框架层坑点（易踩、难查）
- **Starlette 1.0 的 `TemplateResponse` 新签名**：`templates.TemplateResponse(request, "name.html", {"request": request})`——`request` 参数提前，旧签名（`name` 在前）在 Starlette 1.0+ 会报错。所有页面路由与 partial 渲染都用新版。
- **Python 3.12 的 `@staticmethod` + 泛型注解**：`@staticmethod` 方法里用 `list[X]`、`X | None` 作返回值注解会报 `TypeError: 'staticmethod' object is not subscriptable`。文件顶部加 `from __future__ import annotations` 即可（已在 `api/services/*.py` 修复）。
- **CDN 必须用 `cdn.jsdelivr.net`**：Bootstrap 5 与 HTMX 都用 jsdelivr，**不要用 `unpkg.com`**——Edge 浏览器的跟踪防护会拦截 unpkg，导致 HTMX 加载失败、局部刷新不工作。

---

## 8. 配置与依赖

- **配置文件**：`backend/config/model_config.json`（模型/端点，`ENV:` 引用密钥）
- **密钥/环境变量**：`.env`（已 gitignore）
- **LLM 客户端**：`backend/core/llm/client.py`（`LLMClient.call` 同步 / `submit_image`+`wait_job` 异步）
- **提示词目录**：`backend/pipelines/prompts/`（按功能分子目录）
- **数据库**：`bili_db`（SQLite，启动时 `bili_db.init_db()` 建表）
- **后台任务**：`api/services/bili_task_manager.py`（`ThreadPoolExecutor` + 内存任务注册表）

---

*本文档随代码维护；最近一次同步：2026-10-16（新增 §4.11：kb 层无 Web 端点说明；kb 设计见 (4)RAG与信息整合设计）。*
