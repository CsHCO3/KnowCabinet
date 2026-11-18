# community 多来源信息服务

这个目录收纳 QQ、贴吧、手动粘贴等多来源信息的公共能力。检索时不按来源隔离；来源只作为溯源字段。

## 已落地

### `backlog.py` — 备选调研方向

全局清单位置：

```text
knowledgebase/community/backlog/backlog.jsonl
knowledgebase/community/backlog/00-备选调研方向.md
```

手动随手记：

```powershell
python -m backend.main backlog add "https://github.com/xxx/yyy"
python -m backend.main backlog add "标题 | https://example.com/repo | 以后抄它的调度逻辑"
python -m backend.main backlog add "项目名 | https://... | 备注" --source browser --tags 项目,Agent
```

批量导入 txt/md/html/json 里的链接：

```powershell
python -m backend.main backlog import D:\notes\links.txt --source manual-import
type D:\notes\links.txt | python -m backend.main backlog import - --source stdin
```

查看/改状态：

```powershell
python -m backend.main backlog list
python -m backend.main backlog status B20261017-XXXXXX learning --local-path E:\some\repo
```

## `qq/` — QCE 导出与归档管线封装

QQ 现在有正式封装：

```text
backend/services/community/qq/{service.py,cli.py}
api/routers/community_qq.py
```

CLI：

```powershell
python -m backend.main qq export-status
python -m backend.main qq export-dry-run
python -m backend.main qq archive-list
python -m backend.main qq plan "AI Agent 综合交流"
python -m backend.main qq kb-check "AI Agent 综合交流"
python -m backend.main qq verify
```

写操作仍必须显式确认，且默认 dry-run：

```powershell
python -m backend.main qq export --confirm --launch
python -m backend.main qq prune-missing "27贝壳校招互帮互助群" --execute --confirm
python -m backend.main qq analyze-run "AI Agent 综合交流" --execute --confirm --limit 1
python -m backend.main qq kb-add "AI Agent 综合交流" --execute --confirm
```

只读 Web API：

```text
/api/community/qq/export-status
/api/community/qq/export-dry-run
/api/community/qq/archive-list
/api/community/qq/groups/{group}/plan
/api/community/qq/groups/{group}/analyze-status
/api/community/qq/groups/{group}/kb-check
/api/community/qq/verify
```

QQ 流水线程序已迁入 infosystem：

```text
backend/services/community/qq/pipeline
```

档案数据根是：

```text
data/community/qq/pipeline
```

`D:\QQChatExporter\chathistory` 不再作为程序目录被依赖；原始目录保留作迁移对照。详细设计（**本地文档，不在仓库里**）：

`文档以及其它代码/community/QQ导出系统迁移与整合设计.md`

### 导出参数（先复制模板）

导出参数读 `backend/services/community/qq/pipeline/export_groups.json`，里面是 **QQ 号 + 群号 + QCE 安装路径**，属本地真值、已被 `.gitignore` 排除。新机器上第一步：

```powershell
cd backend/services/community/qq/pipeline
copy export_groups.example.json export_groups.json   # 再把 qq_uin / peer_uid / qce_root 换成自己的
```

`options` 里的字段就是 QCE 界面勾的那几个，别自己发明；字段含义见同文件 `_anchors`。

### QQ 原子知识点转换

```powershell
python -m backend.services.community.qq.atomic_import
```

输出：

```text
data/community/qq/knowledge/atomic_entries.jsonl
data/community/qq/knowledge/atomic_import_report.json
```

当前导入 573 条，证据未导入，但每条保留 `source_markers`。

### `tieba/` — 已正式接入

贴吧抓取、搜索、收藏、Cookie 与知识点提炼已接入 `python -m backend.main tieba`。默认只保存知识点和被引用证据，不保存全帖流水；截图默认不看。
