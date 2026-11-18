# 信息处理系统（Infosystem）

Infosystem 是一套个人文献调研、资料处理和知识库工具。可以用命令行跑批量任务，也可以在本机浏览器里操作论文、视频和文档任务。

首次部署可以先跑命令行和 Web。按需再配置文档转换服务器、站点登录态、模型权重和外部模型 API。

## 能做什么

| 功能 | 主要入口 | 额外条件 |
| --- | --- | --- |
| 论文检索、下载和归档 | `arxiv`、`survey`、`paper-search`、`download-papers`、`archive`；Web 的 `/papers`、`/archive` | 公共来源需要能访问相应站点。订阅全文需要相应机构授权。 |
| 本地知识库 | `kb-scan`、`kb-ask`、`kb-fetch`、`kb-embed` | 准备自己的 Markdown 资料。关键词检索使用 SQLite 全文索引。语义检索需安装向量扩展并建立向量索引。 |
| PDF 转 Markdown 和翻译 | `pdf`、`mineru`、`warmup`、`translate`；Web 的 `/tasks/submit` | 需要自备可通过 SSH 访问的转换服务器。MinerU 还支持部分 Office 和图片格式。 |
| 图片文字识别和文本处理 | `ocr`、`ocr-smart`、`condense`、`chat`、`persona`；Web 的 `/ocr` | 按所选流程配置视觉或文本模型。微信 OCR 分支依赖 Windows 组件。 |
| B 站资料整理 | `bili-find`、`bili-survey-stage1`、`bili-visual-scan`、`video-import`；Web 的 `/videos` | 收藏夹、受限内容和部分字幕需要自己的 B 站登录态。抽帧需要 FFmpeg，缺字幕时可加装本地语音转写。按流水线配置分段和视觉模型。 |
| 贴吧、QQ 和调研备选清单 | `tieba`、`qq`、`backlog` | 贴吧的登录态功能需要自己的会话。QQ 导出需要 QQChatExporter（QCE）、本机登录态和授权账号配置。已有导出可按社区文档做离线处理。 |
| 远程文件检索和下载 | `remote-fs-list`、`remote-fs-search`、`remote-fs-download` | 按操作配置有权访问的 SSH、HTTP 或 FTP 服务。 |

CLI 和 Web 可以独立运行。知识库命令返回命中片段及其来源，随后可用 `kb-fetch` 取回原文。

## 本机启动

使用 Python 3.12，建议新建独立环境。下面是 Windows PowerShell 与 Conda 示例，在仓库根目录运行。

```powershell
conda create -n infosystem python=3.12
conda activate infosystem
python -m pip install -r requirements.txt
python -m backend.main --help
python run_api.py
```

打开 [本机 Web](http://127.0.0.1:8765/) 或 [API 文档](http://127.0.0.1:8765/docs)。使用默认的回环地址和单工作进程。任务中心有进程内状态，Web 的任务与设置接口面向本机使用。若要给其他人访问，需要配好访问鉴权、数据隔离和任务管理，然后再开放监听。

Windows 下也可以使用 `server.sh` 管理后台进程。在 Git Bash 中把 `APP_PYTHON` 指向自己的 Python，再执行 `bash server.sh start`。日常可用 `status`、`log` 和 `stop`。前端由 Python 服务渲染模板，可直接启动。Node.js 用于部分站点自动化。

### 部署环境依赖模板

在仓库根目录将 [`.envs_template`](.envs_template) 复制为 `.envs`。模板按功能列出配置项，所有值留空。只填写要使用的功能，`.envs` 由 Git 忽略。已设置的进程环境变量优先。

外接部署主要有两部分。Marker 通过 SSH 调用现有远端脚本。校园出口用于已有、获准使用的网络通道。MinerU、翻译、远程文件、机构下载和云模型是按需启用的附加功能。缺少必要配置时，相应功能会报出缺项。

### 按功能补装

- **公共论文检索**可以先试 `python -m backend.main paper-search "检索词" --no-gs`。省略的 Google Scholar 来源需要具备可用的网络路径。涉及下载时，分别确认目标站是否提供开放全文或你是否有订阅权限。
- **浏览器自动化**按使用的功能安装浏览器运行时。Python 脚本使用 `python -m playwright install chromium`。论文站点的 JavaScript 脚本还需要 Node.js 22 或更新版本、可解析的 npm `playwright` 包和对应浏览器。贴吧的 Node 依赖见其 `package.json`。
- **文本和图像模型 API**将 `backend/config/model_config.example.json` 复制为 `backend/config/model_config.json`，按需填写模型名和服务地址。模板中 `ENV:变量名` 引用的密钥写入本机 `.envs`。模型服务会收到相应流程提交的文本或图片。
- **本地知识库**另装 `jieba`，按下面的示例建立本机配置。语义检索另装 `numpy`、`sqlite-vec` 和 `FlagEmbedding`，运行 `kb-embed` 后用 `kb-ask --mode deep`。文件夹 BM25 检索还需 `rank-bm25`。
- **B 站画面和语音处理**准备 PATH 上的 FFmpeg。画面处理需要 `Pillow`、`numpy`。语音转写需要 `faster-whisper` 及相应权重。使用 GPU 时，按自己的显卡和驱动选择 PyTorch 与 CTranslate2 的 CUDA 运行环境。

### 从空目录建一个知识库

仓库忽略了整个 `knowledgebase/`，因此新克隆要先建立自己的配置。以下是索引普通笔记的最小示例，沿用 `ragsearch/registry.json` 已登记的 `dairy` 目录。

| 文件 | 示例内容 |
| --- | --- |
| `knowledgebase/_meta/config/topics.json` | `{"topics":{"notes":{"kbs":["dairy"]}}}` |
| `knowledgebase/_meta/config/tiers.json` | `{"kinds":{"note":1}}` |
| `knowledgebase/dairy/example.md` | 任意 Markdown 笔记，写入一个可供测试的检索词。 |

```powershell
python -m backend.main kb-scan
python -m backend.main kb-ask "检索词" --pretty
```

扩展目录或索引其它文档类型时，按知识库设计文档登记主题与文档类型。

## 三个本地模型

三个模型都由 Python 在本机加载。按要启用的功能下载权重，默认保存在仓库根目录的 `.models/` 下。

| 官方权重 | 用途 | Python 加载库 | 本地位置与覆盖项 |
| --- | --- | --- | --- |
| [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) | 文本嵌入与语义检索 | `FlagEmbedding` | `.models/bge-m3/`，可用 `BGE_MODEL` 指向权重目录 |
| [Systran/faster-whisper-medium](https://huggingface.co/Systran/faster-whisper-medium) | B 站缺少字幕时转写音频 | `faster-whisper`（使用 CTranslate2） | `.models/asr/` 下的 Hugging Face 缓存，可用 `ASR_MODEL_ROOT` 指定缓存根目录 |
| [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3) | 知识库结果重排、标题与字幕相关性评分 | `transformers` | `.models/bge-reranker-v2-m3/`，可用 `RERANK_MODEL_DIR` 指定权重目录 |

在仓库根目录运行下列命令。每条下载命令对应一个模型，可按需执行。

```powershell
python -m pip install huggingface_hub
python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3', local_dir='.models/bge-m3', ignore_patterns='onnx/*')"
python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-medium', cache_dir='.models/asr')"
python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-v2-m3', local_dir='.models/bge-reranker-v2-m3', ignore_patterns=['onnx/*', 'pytorch_model.bin'])"
```

Whisper 使用 `cache_dir` 保留加载器需要的缓存结构。权重尚未准备好时，可以先用关键词检索和视频已有字幕。

## 远程功能怎么迁移

1. 准备有授权的 Linux GPU 主机和 SSH 账号。在本机 `.envs` 填写自己的 `SSH_HOST`、`SSH_PORT`、`SSH_USERNAME`，并提供 `SSH_KEY_PATH` 或相应凭据。SSH 先尝试直连，失败后尝试 SOCKS 代理。需要代理时，将 `SSH_PROXY_HOST`、`SSH_PROXY_PORT`、`SSH_PROXY_TYPE` 改为你获准使用的设置。
2. 在服务器上安装 Marker，部署仓库提供的 `remote_code/convert.py`。准备 Python 环境和有读写权限的输入、输出、日志目录。若启用 MinerU 或翻译，另行部署兼容的远端脚本和运行环境。
3. Marker 只支持通过 SSH 调用远端脚本。按实际部署填写 `REMOTE_*` 配置。核对解释器、远端脚本、读写目录，以及可选的 GPU 检查与清理脚本。转换先提交一个样本文档，再用 `warmup` 拉结果；Web 也可在任务中心查看状态。
4. 远程文件功能用自己的文件服务。使用 HTTP 或 FTP 的分支时，补齐对应 `REMOTE_FS_HTTP_URL`、`REMOTE_FS_FTP_*` 配置。

现有默认值和部分辅助脚本带有原环境的路径与服务配置。迁移时以 [配置字段](backend/config/settings.py) 为清单，逐项换成本机或自有服务器上的值。

### 哪些功能需要机构权限

- 本地 Web、知识库、公共论文源和自建文档转换服务可以在普通网络环境中使用。
- 某些订阅全文以机构出口 IP 判权。需要具备相应订阅的机构网络或获准使用的访问通道，部分来源还需要站点登录态。
- 学校图书馆的校外访问分支依赖该校的代理协议、域名和登录票据。目前实现与原学校绑定。部署到其他机构需要替换相应适配逻辑，并使用当地认可的认证方式和有权访问的账号。学生、教职工或其他身份是否可用取决于机构授权。
- B 站、贴吧和 QQ 用各自的平台账号。复现收藏夹、历史会话或私有群数据需要原数据所有者授权及相应会话或导出文件。

## 本机数据与提交

`knowledgebase/`、`data/`、`.data/`、`.models/` 和 `.envs` 用于保存本机资料、运行状态、权重和凭据。论文网站的登录态保存在 `data/paper_sessions/<站点>/`。QQ 导出账号和群号填在已忽略的 `export_groups.json`，仓库提供 `export_groups.example.json` 模板。

`.githooks/pre-commit` 是随仓库分发的提交检查脚本。新克隆后可手动启用，也可以直接检查暂存区或已跟踪文件。

```bash
git config core.hooksPath .githooks
python scripts/check_staging.py
python scripts/check_staging.py --audit
```

共享前检查源码、文档和已跟踪配置里的默认值，尤其是主机、服务地址、代理路由、账号和凭据。Git 忽略规则作用于未跟踪文件。已提交过的敏感内容还需处理历史，并轮换相应凭据。

## 代码与文档

- `backend/main.py` 注册 CLI 命令，`backend/services/` 和 `backend/pipelines/` 实现具体服务与处理流程。
- `api/` 提供 Web API，`frontend/` 放置页面模板和静态文件，`remote_code/convert.py` 是随仓库分发的 Marker 远程转换脚本。
- 命令和参数见 [CLI 命令速查](文档以及其它代码/infosystem命令速查.md)，单条命令也可以执行 `python -m backend.main <命令> --help`。
- 页面和接口见 [Web API 设计](文档以及其它代码/frontend-api-design.md)。
- 建库、索引和回源见 [知识库与检索系统设计](文档以及其它代码/%284%29RAG与信息整合设计/知识库与检索系统设计.md)。
- 贴吧、QQ 和调研清单见 [社区服务总览](文档以及其它代码/community/community服务总览.md)。
