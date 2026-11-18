"""万方数据检索源（A1 纯 HTTP，带机构 cookie）。

实现依据（2026-08-12 反查）：
- 万方搜索走 gRPC-Web + protobuf：POST https://s.wanfangdata.com.cn/SearchService.SearchService/search
- 必须带机构 cookie `CASTGC`（CAS 单点登录，北京科技大学）+ gRPC-Web 头。
- 请求体 = gRPC-Web 帧(flag+4B长度) + protobuf（外层 f1=查询消息/f2=页码/f4=AI标记，
  内层 f1="paper"/f2=关键词/f6=每页20/f12="pc"/f13="search"）。
- 响应 protobuf：外层 f4=结果组列表，每组 f101(期刊)/f104(会议)等 = 结果条目，
  含 f1=fileId、f2/f3=标题、f3/f4=作者、f23/f24=期刊、f33=年份、f39=核心标记(PKU/ISTIC/EI)、
  f40=PDF路径、f41=DOI。
- cookie 会过期：失效需重新登录万方（CAS）并重导 data/paper_sessions/wanfang/storage_state.json。
  该文件是本地运行时数据，data/ 已被 .gitignore 排除（不要写回包目录）。
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.services.papers.http_client import ROUTE_DIRECT, http_get, throttle
from backend.services.papers.session_paths import storage_state

logger = logging.getLogger(__name__)

_URL = "https://s.wanfangdata.com.cn/SearchService.SearchService/search"
_SS_FILE = storage_state("wanfang")
_PAGE_SIZE = 20

# ── protobuf 编码 ────────────────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wt: int) -> bytes:
    return _varint((field << 3) | wt)


def _field_len(field: int, data: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(data)) + data


def _field_varint(field: int, val: int) -> bytes:
    return _tag(field, 0) + _varint(val)


# 万方字段名（中文前缀查询串，实测 `作者:(刘阳)`）
_WF_FIELDS = {
    "theme": "主题", "title": "题名", "keyword": "关键词",
    "abstract": "摘要", "author": "作者", "doi": "DOI",
}
# 文献类型代码（实测 Type 子 protobuf 用英文码）
_WF_TYPES = {
    "journal": "Periodical", "thesis": "Thesis", "conference": "Conference",
    "patent": "Patent", "standard": "Standard",
}


def _build_query_msg(
    keyword: str, *, author: str = "", title: str = "", db_types: list[str] | None = None,
    page_size: int = _PAGE_SIZE,
) -> bytes:
    """内层查询消息。f2 = 查询串（带字段前缀）；f4 = Type 过滤子消息（可选）。"""
    # 查询串：纯关键词 或 字段前缀（实测 `作者:(刘阳)`）
    if author or title:
        parts = []
        if keyword:
            parts.append(f"主题:({keyword})")
        if title:
            parts.append(f"题名:({title})")
        if author:
            parts.append(f"作者:({author})")
        q = " AND ".join(parts)
    else:
        q = keyword

    msg = (
        _field_len(1, b"paper")
        + _field_len(2, q.encode("utf-8"))
        + _field_varint(5, 1)
        + _field_varint(6, page_size)
        + _field_len(8, b"\x00")
        + _field_varint(9, 1)
        + _field_len(12, b"pc")
        + _field_len(13, b"search")
    )
    # 文献类型过滤（实测 f4 = 子消息 {1:"Type", 2:'("Periodical" OR "Thesis")'}）
    if db_types:
        codes = [_WF_TYPES.get(t, t) for t in db_types]
        type_query = "(" + " OR ".join(f'"{c}"' for c in codes) + ")"
        type_sub = _field_len(1, b"Type") + _field_len(2, type_query.encode("utf-8"))
        msg += _field_len(4, type_sub)
    return msg


def _build_request(
    keyword: str, page: int = 1,
    *, author: str = "", title: str = "", db_types: list[str] | None = None,
    page_size: int = _PAGE_SIZE,
) -> bytes:
    """完整请求体（gRPC-Web 帧）。"""
    q = _build_query_msg(keyword, author=author, title=title, db_types=db_types, page_size=page_size)
    proto = (
        _field_len(1, q)
        + _field_varint(2, page)
        + _field_len(4, b"AI_READ")
        + _field_len(4, b"AI_EXTRACT")
    )
    return b"\x00" + len(proto).to_bytes(4, "big") + proto


# ── cookie ───────────────────────────────────────

def load_castgc() -> str:
    if not _SS_FILE.exists():
        raise RuntimeError(f"未找到万方 cookie: {_SS_FILE}（需先登录并导出）")
    state = json.loads(_SS_FILE.read_text(encoding="utf-8"))
    for c in state.get("cookies", []):
        if c.get("name") == "CASTGC":
            return c.get("value", "")
    raise RuntimeError("storage_state 中无 CASTGC")


# ── HTTP ─────────────────────────────────────────

def _post_search(
    keyword: str, page: int = 1,
    *, author: str = "", title: str = "", db_types: list[str] | None = None,
    page_size: int = _PAGE_SIZE,
) -> bytes:
    body = _build_request(keyword, page, author=author, title=title, db_types=db_types, page_size=page_size)
    castgc = load_castgc()
    headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "grpc-web-javascript/0.1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
        "cookie": f"CASTGC={castgc};CASTGCSpecial=;",
        "origin": "https://s.wanfangdata.com.cn",
        "referer": f"https://s.wanfangdata.com.cn/paper?q={keyword}&p={page}",
    }
    from urllib.parse import quote
    from urllib.request import Request, urlopen
    headers["referer"] = f"https://s.wanfangdata.com.cn/paper?q={quote(keyword)}&p={page}"
    req = Request(_URL, data=body, headers=headers, method="POST")
    with urlopen(req, timeout=30) as resp:
        return resp.read()


# ── protobuf 解码 ────────────────────────────────

def _decode_varint(b: bytes, i: int):
    val = 0
    shift = 0
    while True:
        byte = b[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, i
        shift += 7


def _is_text(val: bytes) -> bool:
    if not val:
        return False
    try:
        txt = val.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not txt:
        return False
    control = sum(1 for ch in txt if ord(ch) < 32 and ch not in "\n\t\r")
    if control / len(txt) > 0.1:
        return False
    printable = sum(1 for ch in txt if (32 <= ord(ch) < 127) or ord(ch) >= 0x2E80)
    return printable / len(txt) > 0.6


def _parse_fields(b: bytes, depth: int = 0, max_depth: int = 4):
    """解码 protobuf 为字段树。"""
    i = 0
    fields: list[dict[str, Any]] = []
    while i < len(b):
        try:
            tag, i = _decode_varint(b, i)
        except IndexError:
            break
        field = tag >> 3
        wt = tag & 7
        entry: dict[str, Any] = {"field": field, "wt": wt}
        try:
            if wt == 0:
                val, i = _decode_varint(b, i)
                entry["kind"] = "varint"
                entry["val"] = val
            elif wt == 2:
                ln, i = _decode_varint(b, i)
                val = b[i:i + ln]
                i += ln
                entry["len"] = ln
                if _is_text(val):
                    entry["kind"] = "str"
                    entry["val"] = val.decode("utf-8")
                elif depth < max_depth and ln < 50000:
                    entry["kind"] = "msg"
                    entry["children"] = _parse_fields(val, depth + 1, max_depth)
                else:
                    entry["kind"] = "bytes"
            elif wt == 5:
                entry["kind"] = "fixed32"
                i += 4
            elif wt == 1:
                entry["kind"] = "fixed64"
                i += 8
            else:
                entry["kind"] = f"wt{wt}"
                i += 1
        except (IndexError, ValueError):
            entry["kind"] = "trunc"
            i += 1
        fields.append(entry)
    return fields


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


# ── 结果提取（按类型分派）────────────────────────

def _extract_item(item: dict[str, Any], item_type: str, group_link: str) -> dict[str, Any] | None:
    """按结果类型（Periodical/Conference/...）提取统一记录。"""
    strings: dict[int, list[str]] = {}
    varints: dict[int, int] = {}
    for f in item:
        if f["kind"] == "str":
            strings.setdefault(f["field"], []).append(f["val"])
        elif f["kind"] == "varint":
            varints[f["field"]] = f["val"]

    def s(field: int, idx: int = 0) -> str:
        vals = strings.get(field, [])
        return _strip_html(vals[idx]) if idx < len(vals) else ""

    file_id = s(1)
    link = group_link or (f"https://d.wanfangdata.com.cn/periodical/{file_id}" if file_id else "")

    if item_type == "Periodical":
        title = s(2) or s(3)
        authors = [_strip_html(v) for v in strings.get(3, [])]
        venue = s(23) or s(24)
        year = varints.get(33)
        doi = s(41)
        core_marks = [_strip_html(v) for v in strings.get(39, [])]
        pdf_path = s(40)
    elif item_type == "Conference":
        title = s(3) or s(2)
        authors = [_strip_html(v) for v in strings.get(4, [])]
        venue = s(37)  # 会议名
        year = varints.get(28)
        doi = ""
        core_marks = []
        pdf_path = ""
    elif item_type == "Thesis":
        title = s(3) or s(2)
        authors = [_strip_html(v) for v in strings.get(4, [])]
        venue = s(6)  # 学校
        year = varints.get(26)
        doi = ""
        core_marks = []
        pdf_path = ""
    else:
        # 兜底：通用启发式（优先 f3/f2，排除摘要片段）
        def _looks_like_title(cand: str) -> bool:
            if not cand:
                return False
            if len(cand) > 50:
                return False
            if cand.startswith(("的", "了", "本文", "随着", "在", "从", "其", "因此", "本", "该")):
                return False
            return not cand.isascii() or len(cand) > 15
        title = ""
        for fld in (3, 2, 4):
            for v in strings.get(fld, []):
                cand = _strip_html(v)
                if _looks_like_title(cand) and len(cand) > len(title):
                    title = cand
        if not title:
            for fld, vals in strings.items():
                for v in vals:
                    cand = _strip_html(v)
                    if _looks_like_title(cand) and len(cand) > len(title):
                        title = cand
        seen_a: list[str] = []
        authors = [v for v in strings.get(3, []) + strings.get(4, [])
                   if 2 <= len(v) <= 12 and not v.isascii() and v not in seen_a
                   and not seen_a.append(v)]
        venue = ""
        year = varints.get(33) or varints.get(28)
        doi = s(41) if strings.get(41) else ""
        core_marks = [_strip_html(v) for v in strings.get(39, [])]
        pdf_path = ""

    if not title:
        return None
    authors = [a for a in authors if a and a not in title][:8]
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": doi.lower() if doi else "",
        "file_id": file_id,
        "core_marks": core_marks,
        "pdf_path": pdf_path,
        "link": link,
        "source": "wanfang",
        "type": item_type,
    }


def _parse_response(body: bytes) -> list[dict[str, Any]]:
    """解析 gRPC-Web 响应 → 统一记录列表。"""
    if len(body) > 5 and body[0] == 0:
        n = int.from_bytes(body[1:5], "big")
        proto = body[5:5 + n]
    else:
        proto = body
    top = _parse_fields(proto, max_depth=3)
    results: list[dict[str, Any]] = []
    for grp in top:
        if grp["kind"] != "msg":
            continue
        # 结果组：f1=类型字符串，f3=详情页 token，f101+ = 条目
        gstr = {f["field"]: f["val"] for f in grp.get("children", [])
                if f["kind"] == "str"}
        item_type = gstr.get(1, "")
        group_link = gstr.get(3, "")
        if group_link:
            group_link = f"https://d.wanfangdata.com.cn/periodical/{group_link}"
        for gfield in grp.get("children", []):
            if gfield["kind"] == "msg" and gfield["field"] >= 100:
                rec = _extract_item(gfield.get("children", []), item_type, group_link)
                if rec:
                    results.append(rec)
                    break  # 每组取一个条目
    return results


# ── 对外接口 ─────────────────────────────────────

def search_records(
    keyword: str, *, rows: int = 20,
    author: str = "", title: str = "", db_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按关键词搜万方（A1 纯 HTTP）。返回统一 dict 列表。cookie 失效抛异常。

    高级选项：author/title（中文前缀查询串 `作者:(...)`）、db_types（文献类型过滤，
    如 ["journal","thesis","conference"]，英文码见 _WF_TYPES）。
    """
    throttle("wanfang", 1.0)
    try:
        body = _post_search(keyword, page=1, author=author, title=title, db_types=db_types)
    except Exception as e:
        logger.warning("万方搜索失败: %s", e)
        return []
    results = _parse_response(body)
    if not results:
        # 可能 cookie 失效（响应缺 auth-context）
        logger.warning("万方无结果（可能 cookie 失效，需重新登录导出）")
    return results[:rows]


def probe() -> dict:
    """检测万方会话状态：cookie 文件、CASTGC、真实搜索探测。

    Returns:
        {"ok": bool, "file": str, "configured": bool, "has_castgc": bool,
         "results": int|None, "error": str|None}
    """
    info = {"file": str(_SS_FILE), "configured": False, "has_castgc": False}
    if not _SS_FILE.exists():
        return {**info, "ok": False, "error": "未找到 cookie 文件，需先登录并导出"}
    try:
        state = json.loads(_SS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {**info, "ok": False, "error": f"cookie 文件解析失败: {e}"}
    info["has_castgc"] = any(c.get("name") == "CASTGC" for c in state.get("cookies", []))
    if not info["has_castgc"]:
        return {**info, "ok": False, "error": "storage_state 中无 CASTGC cookie，需重新登录导出"}
    info["configured"] = True
    # 真实探测：最小一次搜索（page_size=1，验证 CASTGC 是否仍有效）
    try:
        body = _post_search("test", page=1, page_size=1)
        results = _parse_response(body)
        if not results:
            return {**info, "ok": False, "error": "无结果（cookie 可能已失效）"}
        return {**info, "ok": True, "results": len(results)}
    except Exception as e:
        return {**info, "ok": False, "error": f"网络/请求失败: {e}"}
