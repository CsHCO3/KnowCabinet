"""私域知识库 MCP server（stdio）——六个 tool 的薄包装。

启动::

    D:/Anaconda/envs/infosystem/python.exe -m backend.services.kb.mcp_server

设计（02 §11 / 05 号接口定稿）：
- **能力只实现一次**：本文件不含检索/治理逻辑，全部转调 `backend/services/kb/`
  的 Python API（CLI 是同一批函数的另一层壳）。换 agent 平台 = 换注册方式。
- **server 名 `kb`**；六个 tool 参数各 ≤6（04 §8-M6 硬门槛）。
- **常驻与惰性**：bge 模型由 `ragsearch.engine.bge_embed` 进程内单例缓存，
  首次 deep 才加载、之后常驻；不 deep 就永远不加载（server 冷启动 <5s 不含模型）。
- **错误语义**：未知 doc_id / 非法 mode / 无回链晋升等一律返回
  `{"ok": false, "error": {"code", "message", ...}}` 的 **JSON 文本**，
  `is_error` 保持 false —— 让 agent 能解析 code 自行决策，而不是拿到一句异常。
- **写纪律**：kb_tag / kb_promote / kb_batch_confirm 走 `db.write_retry`
  （短事务 + busy_timeout 重试 3 次），单写者收敛。

MCP SDK 版本说明：本机装的是 `mcp` 2.0.0，其高层类名是
`mcp.server.mcpserver.MCPServer`（1.x 的 `mcp.server.fastmcp.FastMCP` 已移除）。
本文件用**兼容导入**同时吃两代 SDK。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ── 路径引导（`python -m backend.services.kb.mcp_server` 之外也支持直接跑）──
_root = Path(__file__).resolve()
for _parent in _root.parents:
    if (_parent / "CLAUDE.md").exists() or (_parent / "README.md").exists():
        _root = _parent
        break
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
# ─────────────────────────────────────────────────────────────────────────────

if sys.platform == "win32":  # type: ignore[attr-defined]
    # 导入期不得替换调用方的 stdio 对象（M3 P2-2 定的规矩）：只原地调编码，
    # 否则会把 FastMCP/pytest 的 wrapper 换掉。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
            _stream.reconfigure(encoding="utf-8")

SERVER_NAME = "kb"
SERVER_VERSION = "1.0.0"

TOOL_NAMES = ("kb_ask", "kb_inventory", "kb_fetch", "kb_tag", "kb_promote",
              "kb_batch_confirm")

_MCP = None


def _import_server_class():
    """兼容 mcp 2.x（MCPServer）与 1.x（FastMCP）。"""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0

        return MCPServer
    except Exception:  # pragma: no cover — 旧 SDK 兜底
        from mcp.server.fastmcp import FastMCP  # type: ignore

        return FastMCP


def get_mcp():
    """取（并惰性创建）MCP server 单例。"""
    global _MCP
    if _MCP is None:
        cls = _import_server_class()
        try:
            _MCP = cls(SERVER_NAME, version=SERVER_VERSION)
        except TypeError:  # pragma: no cover — 1.x 不接受 version
            _MCP = cls(SERVER_NAME)
        _register_tools(_MCP)
    return _MCP


# ── 统一返回与错误语义 ───────────────────────────────────────────────────────


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _ok(**payload) -> str:
    return _json({"ok": True, **payload})


def fail(code: str, message: str | None = None, **extra) -> str:
    """结构化错误（04 §8-M6 ③）：`{ok:false, error:{code,message}}`。"""
    return _json({"ok": False, "error": {"code": code, "message": message or code, **extra}})


# query.fetch 的错误码 → MCP 错误码（保留原码，补 message）
_FETCH_MESSAGES = {
    "not_found": "未找到该 doc_id（检查是否跨 topic 撞名，必要时用 topic:id）",
    "ambiguous_doc_id": "裸 id 在多 topic 命中，请用带 topic 前缀的存储 ID 重试",
    "tier_not_available": "该文档没有所请求的档位版本",
    "read_failed": "版本路径读取失败（文件可能已移动）",
    "invalid_chunk_id": "chunk_id 非法（必须是整数）",
    "chunk_not_found": "chunk_id 不属于该文档；请用 kb_ask 的 matched_chunks[].chunk_id",
}


def _map_error(result: dict, table: dict[str, str] | None = None) -> str:
    code = str(result.get("error") or "error")
    message = (table or {}).get(code) or result.get("hint") or code
    extra = {key: value for key, value in result.items()
             if key not in ("error", "hint", "message")}
    return fail(code, message, **extra)


# ── 六个 tool（全部是薄包装）───────────────────────────────────────────────


def kb_ask(query: str = "", topic: str | None = None, tags: str | None = None,
           org: str | None = None, mode: str = "balanced", top_k: int = 10) -> str:
    """检索知识库（browse=我有什么 / balanced=默认 / deep=要细节，deep 才走向量）。

    Args:
        query: 查询串（中文自动分词）。
        topic: 主题过滤，如 cbac / papers / ai-agent。
        tags: 标签过滤，逗号分隔，任一命中。
        org: 项目线/组织标记过滤，逗号分隔，任一命中。
        mode: browse | balanced | deep。
        top_k: 返回条数（默认 10，上限 50）。
    """
    from backend.services.kb.query import MODES, ask

    if mode not in MODES:
        return fail("invalid_mode", f"非法 mode：{mode}（expect {'|'.join(MODES)}）")
    try:
        top_k = int(top_k) if top_k is not None else 10
    except (TypeError, ValueError):
        return fail("invalid_argument", f"top_k 必须是整数，收到 {top_k!r}")
    if top_k < 1 or top_k > 50:
        return fail("invalid_argument", "top_k 必须在 1..50 之间")
    if not (query or "").strip():
        return fail("invalid_argument", "query 不能为空")
    try:
        result = ask(
            query,
            topic=topic or None,
            tags=_csv(tags),
            org=_csv(org),
            mode=mode,
            top_k=top_k,
        )
    except ValueError as exc:
        return fail("invalid_argument", str(exc))
    except Exception as exc:  # noqa: BLE001 — server 不崩是硬要求
        return fail("internal_error", f"{type(exc).__name__}: {exc}")
    # 精简给 LLM 的负载：matched_chunks 只留取证必需的分钟字段
    return _ok(**result, tool="kb_ask", evidence_rule=_EVIDENCE_RULE)


def kb_inventory(topic: str | None = None, tag: str | None = None) -> str:
    """库存体检：返回紧凑人类可读文本 + snapshot JSON（调研前先看这个）。

    Args:
        topic: 主题过滤，如 cbac；留空看全库。
        tag: 标签过滤（canonical）。
    """
    from backend.services.kb.inventory import render_compact, snapshot

    try:
        snap = snapshot(topic or None, tag or None)
        return _ok(tool="kb_inventory", compact=render_compact(snap), snapshot=snap)
    except Exception as exc:  # noqa: BLE001
        return fail("internal_error", f"{type(exc).__name__}: {exc}")


def kb_fetch(doc_id: str = "", tier: int | None = None, chunk_ids: str | None = None) -> str:
    """取证：取某文档某一档正文；给 chunk_ids 则只回命中块 ± 上下文（推荐）。

    Args:
        doc_id: 存储 ID（跨 topic 撞名时为 topic:id）。
        tier: 指定档位 0-3；留空取最高档。
        chunk_ids: 块号，逗号分隔（来自 kb_ask 的 matched_chunks[].chunk_id）。
    """
    from backend.services.kb.query import fetch

    if not (doc_id or "").strip():
        return fail("invalid_argument", "doc_id 不能为空")
    try:
        parsed_tier = None if tier in (None, "", -1) else int(tier)
    except (TypeError, ValueError):
        return fail("invalid_argument", f"tier 必须是 0-3 的整数，收到 {tier!r}")
    if parsed_tier is not None and parsed_tier not in (0, 1, 2, 3):
        return fail("invalid_argument", f"tier 必须在 0..3，收到 {tier!r}")
    try:
        result = fetch(doc_id, tier=parsed_tier, chunk_ids=_csv(chunk_ids))
    except Exception as exc:  # noqa: BLE001
        return fail("internal_error", f"{type(exc).__name__}: {exc}")
    if "error" in result:
        return _map_error(result, _FETCH_MESSAGES)
    return _ok(tool="kb_fetch", **result)


def kb_tag(doc_ids: str = "", add: str | None = None, remove: str | None = None) -> str:
    """给文档增删 tags（**只写 kb.db 词表，绝不改 md 文件**）。

    Args:
        doc_ids: 存储 ID，逗号分隔。
        add: 要加的标签，逗号分隔（按各 doc 的 topic 词表 canonical 归一）。
        remove: 要删的标签，逗号分隔。
    """
    from backend.services.kb.tags import tag_docs

    ids = _csv(doc_ids)
    if not ids:
        return fail("invalid_argument", "doc_ids 不能为空")
    add_values, remove_values = _csv(add), _csv(remove)
    if not add_values and not remove_values:
        return fail("invalid_argument", "add 与 remove 至少要给一个")
    try:
        report = tag_docs(ids, add=add_values, remove=remove_values)
    except Exception as exc:  # noqa: BLE001
        return fail("internal_error", f"{type(exc).__name__}: {exc}")
    errors = report.get("errors") or []
    if report.get("not_found"):
        return fail("unknown_doc", f"未找到 doc_id：{', '.join(report['not_found'])}",
                    report=report)
    if report.get("ambiguous"):
        return fail("ambiguous_doc_id", "裸 id 多 topic 命中，请用带 topic 前缀的存储 ID",
                    report=report)
    if errors:
        return fail(str(errors[0].get("code") or "db_error"),
                    str(errors[0].get("message") or "写库失败"), report=report)
    return _ok(tool="kb_tag", md_written=report.get("md_written", 0),
               updated=report.get("updated", []), changed=report.get("changed", []),
               noop=report.get("noop", []), report=report)


def kb_promote(doc_ids: str = "", to_tier: int = 2) -> str:
    """晋升校验并落库：to_tier=2 必须带回链（否则 missing_backlink），3 须有 L3 版本。

    Args:
        doc_ids: 存储 ID，逗号分隔。
        to_tier: 目标档位，2 或 3。
    """
    from backend.services.kb.promote import promote

    ids = _csv(doc_ids)
    if not ids:
        return fail("invalid_argument", "doc_ids 不能为空")
    try:
        tier = int(to_tier)
    except (TypeError, ValueError):
        return fail("invalid_argument", f"to_tier 必须是 2 或 3，收到 {to_tier!r}")
    if tier not in (2, 3):
        return fail("invalid_argument", f"to_tier 必须是 2 或 3，收到 {to_tier!r}")
    try:
        report = promote(ids, to_tier=tier, apply=True, strict=True)
    except Exception as exc:  # noqa: BLE001
        return fail("internal_error", f"{type(exc).__name__}: {exc}")
    rejected = report.get("rejected") or []
    reasons = {item.get("reason") for item in rejected}
    if "missing_backlink" in reasons:
        return fail("missing_backlink",
                    "无回链不得晋升 L2（D27）：L2 节点必须挂 [L3:路径#chunk] 回链",
                    report=report)
    if "not_found" in reasons:
        missing = [item["doc_id"] for item in rejected if item.get("reason") == "not_found"]
        return fail("unknown_doc", f"未找到 doc_id：{', '.join(missing)}", report=report)
    if reasons:
        return fail(str(sorted(reasons)[0]), "晋升被拒", report=report)
    return _ok(tool="kb_promote", **report)


def kb_batch_confirm(round_id: str = "", topic: str = "", tags: str = "",
                     doc_ids: str | None = None, action: str = "auto") -> str:
    """批次（候选区）确认：把外搜攒下的一批一次性入库（D12）。

    Args:
        round_id: 批次号（`auto` 时可留空，会自动生成）。
        topic: 主归属主题（必填，确认后写入本批全部条目）。
        tags: 本批统一标签，逗号分隔（按 topic 词表 canonical 归一）。
        doc_ids: 要加入本批的存储 ID，逗号分隔（先 add 再 confirm）。
        action: auto | open | add | confirm | list（默认 auto）。
    """
    from backend.services.kb.batch import lifecycle

    mode = (action or "auto").strip().lower()
    if mode not in ("auto", "open", "add", "confirm", "list"):
        return fail("invalid_argument",
                    f"action 非法：{action}（expect auto|open|add|confirm|list）")
    if mode not in ("list", "open") and not (topic or "").strip():
        return fail("invalid_argument", "topic 不能为空（主归属必填，D24）")
    try:
        result = lifecycle((round_id or "").strip() or None, topic or "", _csv(tags),
                           doc_ids=_csv(doc_ids), action=mode)
    except Exception as exc:  # noqa: BLE001
        return fail("internal_error", f"{type(exc).__name__}: {exc}")
    if "error" in result:
        extra = {key: value for key, value in result.items() if key != "error"}
        return _map_error({"error": result["error"], **extra}, _BATCH_MESSAGES)
    return _ok(tool="kb_batch_confirm", **result)


_BATCH_MESSAGES = {
    "invalid_argument": "参数非法",
    "unknown_round": "批次不存在（用 action=open 开批，或直接给 doc_ids 走 auto）",
    "already_confirmed": "该批次已确认，未重复写入",
    "empty_round": "候选区为空，无需确认",
    "unknown_doc": "批次里有 doc_id 不在库中，请先入库再确认",
}


# ── 证据双轨铁律（随每次 kb_ask 返回，防止 agent 只拿片段就下结论）────────────

_EVIDENCE_RULE = (
    "证据双轨（D44 口径）：先看 evidence.quote_from —— "
    "raw=该文档有原文版本，引用前用 kb_fetch(doc_id, tier=evidence.quote_tier, "
    "chunk_ids=quote_chunk_ids[0]) 取原话；"
    "derived=只有轻整理/提炼版，只能转述、不可当原话引用；"
    "none 或 l0_only=true 只能表述为「存在这篇（仅元数据）」，禁止把 abstract 当结论引用。"
    "legacy_l2=true 是 D27 存量豁免，按 derived 对待即可。"
)


def _csv(value) -> list[str]:
    """`a,b` / `a, b` → ['a','b']；空值给空列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("，", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _register_tools(server) -> None:
    """注册六个 tool（显式取名，避免装饰器把 `_tool_` 前缀带进 schema）。"""
    for fn in (kb_ask, kb_inventory, kb_fetch, kb_tag, kb_promote, kb_batch_confirm):
        server.tool(name=fn.__name__, description=_first_line(fn.__doc__))(fn)


def _first_line(doc: str | None) -> str:
    text = (doc or "").strip()
    return text.splitlines()[0].strip() if text else ""


def tool_schemas() -> list[dict]:
    """六个 tool 的 name/参数名/参数个数（验收①用；不依赖 MCP 运行时）。"""
    import inspect

    out = []
    for fn in (kb_ask, kb_inventory, kb_fetch, kb_tag, kb_promote, kb_batch_confirm):
        params = list(inspect.signature(fn).parameters)
        out.append({"name": fn.__name__, "parameters": params, "param_count": len(params)})
    return out


def main() -> None:
    server = get_mcp()
    run = getattr(server, "run", None)
    if run is None:  # pragma: no cover
        raise SystemExit("MCP server 对象没有 run()，SDK 版本不受支持")
    run()


if __name__ == "__main__":
    main()
