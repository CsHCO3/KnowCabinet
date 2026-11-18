#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQChatExporter 导出 JSON 解析核心（被 archive.py 复用，也可单独跑）。

对外主要接口
------------
  Cleaner(src, ...).parse_timeline() -> TimelineItem 列表（带时间戳、稳定映射）

单独使用（一次性清洗成 md）：
    python clean_json.py -i <导出.json> [-o out.md]
细节见 --help。

OCR 不在这里做（R39/R40）：解析聊天记录时不允许触发 OCR。
    手动的唯一入口：python analyze.py ocr --group 群名 [--batch 2-4] [--since/--until …]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# ────────────────────────── 可调限制（硬上限，防止无边界调用） ──────────────────────────
OCR_DEFAULT_MAX = 30          # --ocr 不给上限时的默认总调用次数
OCR_HARD_MAX = 200            # 硬上限：单次运行最多调用次数
OCR_TIMEOUT_SEC = 40          # 单张图片 OCR 超时秒数
OCR_SUMMARY_CHARS = 50        # 摘要概览字数上限
OCR_ENGINE_START_WAIT = 15    # 等 OCR 服务连接成功上限（秒）
OCR_ENGINE_READY_TIMEOUT = 60  # 引擎初始化（启动 WeChatOCR.exe）上限（秒）
MAX_TEXT_CHARS = 4000         # 单条消息文字截断

# ────────────────────────── OCR 环境常量（本机实测路径） ──────────────────────────
WECHAT_OCR_EXE = os.path.join(
    os.environ.get("APPDATA", r"C:\Users\HP\AppData\Roaming"),
    r"Tencent\WeChat\XPlugin\Plugins\WeChatOCR\7079\extracted\WeChatOCR.exe",
)
WECHAT_DIR = r"C:\Program Files\Tencent\WeChat\[3.9.12.55]"

DROP_TYPES = {"system"}
DEFAULT_BLACKLIST = Path(__file__).resolve().parent.parent / "blacklist.txt"


def load_blacklist(fp: Path | str | None = None) -> dict[str, list[str]]:
    """黑名单规则（命中就整条消息丢弃，不进档案）。文件形如：

        [text]      消息正文（含 ↩ 引用内容）里出现该字符串 → 丢
        [who]       发送者用户名、或消息里 @ 到的对象，等于该字符串 → 丢

    一行一条，子串匹配、不区分大小写，`#` 开头是注释。缺文件=空规则。
    """
    out = {"text": [], "who": []}
    p = Path(fp) if fp else DEFAULT_BLACKLIST
    if not p.is_file():
        return out
    sec = None
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(text|who)\]$", line, re.I)
        if m:
            sec = m.group(1).lower()
            continue
        if sec:
            out[sec].append(line)
    return out


def blacklist_hit(text: str, sender: str, mentions: list[str],
                  bl: dict[str, list[str]]) -> str:
    """返回命中的规则（`text:xxx` / `who:xxx`），没命中返回空串。"""
    if not bl:
        return ""
    low = text.lower()
    for pat in bl.get("text") or []:
        if pat.lower() in low:
            return f"text:{pat}"
    names = [sender] + [m for m in mentions if m]
    for pat in bl.get("who") or []:
        pl = pat.lower()
        for n in names:
            if n and pl in n.lower():
                return f"who:{pat}"
    return ""

MD5_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{32}_")
# OCR 噪声块：纯时间/纯日期/纯数字符号（截图里的 "12:31"、"*5*553"）
NOISE_BLOCK_RE = re.compile(
    r"^\W*(\d{1,2}:\d{2}(:\d{2})?|\d{4}[/-]\d{1,2}[/-]\d{1,2}|[\d*+\-._/]{3,})\W*$"
)


# ══════════════════════════════ 基础工具 ══════════════════════════════

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def letter_label(idx: int) -> str:
    """0->A ... 25->Z, 26->A1, 27->B1 ..."""
    return chr(ord("A") + idx) if idx < 26 else chr(ord("A") + idx % 26) + str(idx // 26)


def clean_filename(p: str) -> str:
    """去掉 md5 前缀，只留文件名（不含 uri/目录）。"""
    if not p:
        return ""
    name = str(p).replace("\\", "/").rsplit("/", 1)[-1]
    return MD5_PREFIX_RE.sub("", name)


def collapse_ws(s: str) -> str:
    """把正文里的空白与**所有会被 splitlines() 当成换行的字符**压成单空格。

    为什么类里要手写 \\x1c \\x1d \\x1e \\x0b \\x0c：Python 的 `\\s` 不含 \\x1c-\\x1e，
    而 `str.splitlines()` 会把它们当换行 —— 实测（2026-10-08）QQ 的"文本炸弹"表情序列
    `<$ǿĀ\\x1a\\x0e>` 里带 \\x0b \\x1c，写进 消息.tsv 后被读回时劈成 14 段、
    两段都凑不够 5 个字段 → **8 条消息整条静默消失**（21888 行写进去只读回 21880 行）。
    源头就把它们压成空格，`save_index`/`load_index` 那边再兜一层（见 archive._CELL_BREAK_RE）。
    """
    return re.sub(r"[ \t\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\u3000]+", " ", s or "").strip()


def short_summary(s: str, limit: int = OCR_SUMMARY_CHARS) -> str:
    """把 OCR 全文压成一段 ≤limit 字的概览（噪声过滤 + 逐词贪心，严格不超长）。"""
    s = re.sub(r"[\n\r\t]+", " ", s)
    toks = [t.strip() for t in s.split(" ") if t.strip()]
    toks = [t for t in toks if not NOISE_BLOCK_RE.match(t)]
    s = collapse_ws(" ".join(toks))
    if not s:
        return ""
    if len(s) <= limit:
        return s
    units = [p for p in re.split(r"(?<=[。！？!?；;，,.])", s) if p.strip()]
    out = ""
    for p in units:
        if len(out) + len(p) <= limit:
            out += p
            continue
        words = [w for w in re.split(r"\s+", p) if w]
        if len(words) > 1:
            for w in words:
                if len(out) + len(w) + 1 > limit:
                    break
                out += (" " if out and not out.endswith(" ") else "") + w
        else:
            room = limit - len(out) - 1
            if room >= 4:
                out += p[:room]
        break
    out = out.rstrip("，,。.；;、 ")
    return (out + "…")[:limit] if len(out) < len(s) else out[:limit]


# ══════════════════════════════ OCR 引擎 ══════════════════════════════

class WeChatOcrEngine:
    """wechat_ocr.OcrManager 最小封装：一次一张，事件驱动等待 + 超时。"""

    def __init__(self, mgr, connect_timeout: int = OCR_ENGINE_START_WAIT):
        self._mgr = mgr
        self._ev: threading.Event | None = None
        self._res: dict | None = None
        self._cur_path = ""
        self._lock = threading.Lock()
        mgr.SetOcrResultCallback(self._on_result)
        t0 = time.time()
        while time.time() - t0 < connect_timeout:
            try:
                if getattr(mgr, "m_connect_state").value:
                    return
            except Exception:
                break
            time.sleep(0.2)
        raise TimeoutError("OCR 服务未连接")

    def _on_result(self, img_path: str, results: dict) -> None:
        with self._lock:
            if self._ev is not None and str(img_path) == self._cur_path:
                self._res = results
                self._ev.set()

    def recognize(self, img_path: str, timeout: int | None = None) -> dict | None:
        timeout = OCR_TIMEOUT_SEC if timeout is None else timeout
        ev = threading.Event()
        with self._lock:
            self._ev, self._res, self._cur_path = ev, None, str(img_path)
        self._mgr.DoOCRTask(str(img_path))
        if not ev.wait(timeout):
            with self._lock:
                self._ev = None
            raise TimeoutError(f"OCR 结果等待超时 {timeout}s")
        with self._lock:
            self._ev = None
            return self._res

    def close(self) -> None:
        for fn in (lambda: self._mgr.SetOcrResultCallback(None), self._mgr.KillWeChatOCR):
            try:
                fn()
            except Exception:
                pass


def start_ocr_engine():
    """启动微信 OCR 引擎；失败返回 None（调用方降级即可）。"""
    if not Path(WECHAT_OCR_EXE).is_file():
        log(f"[OCR] 找不到 WeChatOCR.exe: {WECHAT_OCR_EXE}")
        return None
    t0 = time.time()
    while time.time() - t0 < OCR_ENGINE_READY_TIMEOUT:
        try:
            from wechat_ocr.ocr_manager import OcrManager  # noqa: WPS433
            with contextlib.redirect_stdout(sys.stderr):
                mgr = OcrManager(WECHAT_DIR)
                mgr.SetExePath(WECHAT_OCR_EXE)
                mgr.SetUsrLibDir(WECHAT_DIR)
                mgr.StartWeChatOCR()
                engine = WeChatOcrEngine(mgr)
            log(f"[OCR] 引擎就绪（{time.time() - t0:.1f}s）")
            return engine
        except Exception as e:
            log(f"[OCR] 引擎启动重试: {e}")
            time.sleep(2)
    log("[OCR] 引擎启动超时")
    return None


def ocr_image_full(engine, img_path: Path, timeout: int | None = None) -> dict | None:
    """识别一张图片 -> {'lines': [...], 'full': '整段文本', 'summary': '≤50 字概览'}。

    完整文本一定要留（存进 ocr_full.json），50 字摘要只是给"记录.md"里方便扫一眼用的；
    喂给提炼模型时只给"值得看图"的指引，不塞全文。
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            raw = engine.recognize(str(img_path), timeout=timeout)
    except TimeoutError:
        log(f"[OCR] 超时跳过 {img_path.name}")
        return None
    except Exception as e:
        log(f"[OCR] 识别异常 {img_path.name}: {e}")
        return None
    items = raw.get("ocrResult") if isinstance(raw, dict) else None
    if not items:
        log(f"[OCR] 无结果 {img_path.name}")
        return None
    try:
        ordered = sorted(items, key=lambda x: (x.get("location", {}).get("top", 0),
                                               x.get("location", {}).get("left", 0)))
        lines = [collapse_ws(it.get("text") or "") for it in ordered]
    except Exception:
        lines = [collapse_ws(str(it)) for it in items]
    lines = [l for l in lines if l]
    if not lines:
        return None
    full = "\n".join(lines)
    return {"lines": lines, "full": full, "summary": short_summary(full)}


def ocr_image(engine, img_path: Path, timeout: int | None = None) -> str:
    """识别一张图片 -> ≤50 字概览（失败返回 ""）。"""
    r = ocr_image_full(engine, img_path, timeout)
    return (r or {}).get("summary", "")


# ══════════════════════════════ 解析核心 ══════════════════════════════

@dataclass
class TimelineItem:
    """一条被保留的消息（用于长期归档，保留时间戳）。"""
    ts: int                       # epoch 毫秒
    time_local: str               # 'YYYY-MM-DD HH:MM:SS'（UTC+8）
    sender_name: str              # 原始用户名（归档层再映射成字母）
    text: str
    media: list[tuple[str, str]] = field(default_factory=list)  # [(kind, basename)]
    mtype: str = ""
    mid: str = ""
    ocr: list[str] = field(default_factory=list)

    def media_marks(self) -> str:
        return " ".join(f"[{'图片' if k == 'image' else '文件'}:{n}]" for k, n in self.media)


class Cleaner:
    """解析一次导出；媒体引用默认全留（R37），只有「整份导出都没带媒体文件」时才不记（R44）。"""

    def __init__(self, src: Path, keep_json_share: bool = False, keep_system: bool = False,
                 media_root: Path | None = None, extras: dict | None = None,
                 blacklist: dict[str, list[str]] | None = None,
                 extra_media: set[str] | None = None):
        self.src = Path(src)
        self.res_dir = Path(media_root) if media_root else self.src.parent / "resources"
        self.keep_json_share = keep_json_share
        self.keep_system = keep_system
        self.extras = extras or {}
        self.blacklist = blacklist or {}
        self.blacklist_hits: list[dict] = []
        # 本群「已归档」的媒体名（上次导入已把文件搬进 batches/**/resources）。
        # 只看导出目录是不够的：文件可能早被上一次导入搬走了，那种引用不算「被删」。
        self.extra_media: set[str] = {str(x).lower() for x in (extra_media or ())}
        # 本次导出「被引用过」的媒体名。导入时只搬这些 —— 绝不可整目录搬，
        # 因为 QQChatExporter 的 resources 是所有群共用的一个目录（见 archive_media）。
        self.referenced_media: set[str] = set()
        self.raw = json.loads(self.src.read_text(encoding="utf-8"))
        self.messages = self.raw.get("messages") or []
        # R44：这次导出到底带没带媒体文件？判据 = **元素自己身上的 localPath**。
        # 不能用"resources/ 目录里有没有文件"来判断 —— 那个目录是所有群共用的
        # （实测 2026-10-08：AI 群的导出没带媒体，同目录里却躺着求职群刚下载的 88 张图）。
        # localPath 是导出器在"勾了导出媒体"时才会写进元素的，只有它是可靠判据。
        self.media_elements_total = sum(
            1 for m in self.messages
            for el in ((m.get("content") or {}).get("elements") or [])
            if el.get("type") in ("image", "file"))
        self.export_has_media_files = any(
            (el.get("data") or {}).get("localPath")
            for m in self.messages
            for el in ((m.get("content") or {}).get("elements") or [])
            if el.get("type") in ("image", "file"))
        self.stats = {
            "total": len(self.messages), "kept": 0,
            "dropped_system": 0, "dropped_recalled": 0, "dropped_empty": 0,
            "media_ok": 0, "media_missing": 0, "media_unexported": 0,
            "dropped_blacklist": 0,
        }
        self._id_index: dict[str, tuple[str, str]] = {}
        self._sent_names: list[str] = []
        self._name_seq: dict[str, str] = {}

    # ---------- 用户名 -> 字母（支持外部既有映射，保证跨批次稳定） ----------
    def assign_name_map(self, existing: dict[str, str] | None = None) -> dict[str, str]:
        """existing: 名字 -> 字母。已有的保持字母不变，新名字往后追加。"""
        mapping = dict(existing or {})
        used = set(mapping.values())
        next_idx = 0

        def next_letter() -> str:
            nonlocal next_idx
            while True:
                lab = letter_label(next_idx)
                next_idx += 1
                if lab not in used:
                    used.add(lab)
                    return lab

        stats = ((self.raw.get("statistics") or {}).get("senders")) or []
        ordered: list[str] = []
        for s in stats:
            n = s.get("name")
            if n and n not in ordered:
                ordered.append(n)
        for m in self.messages:
            n = (m.get("sender") or {}).get("name")
            if n and n not in ordered:
                ordered.append(n)
        for n in ordered:
            if not self._is_real_name(n):
                continue
            if n not in mapping:
                mapping[n] = next_letter()
        self._name_seq = mapping
        self._sent_names = [n for n in ordered]
        # uid -> 名字（消息里 sender.uid 可能带名字，reply 里只有 senderUin）
        self._uid_to_name = {}
        for m in self.messages:
            sd = m.get("sender") or {}
            if sd.get("uid") and sd.get("name"):
                self._uid_to_name[sd["uid"]] = sd["name"]
            if sd.get("uin") and sd.get("name"):
                self._uid_to_name[str(sd["uin"])] = sd["name"]
        return mapping

    @staticmethod
    def _is_real_name(n: str) -> bool:
        return bool(n) and not n.startswith("u_") and n not in ("未知", "系统消息", "系统")

    def label_of_name(self, name: str) -> str:
        if name in self._name_seq:
            return self._name_seq[name]
        # 没进映射（系统/未知）时给个稳定兜底
        return "?"

    # ---------- 媒体存在性 ----------
    def media_exists(self, local_path: str) -> tuple[bool, str]:
        """文件在导出目录里，或**已归档在本群 batches 里**，都算存在。"""
        if not local_path:
            return False, ""
        rel = str(local_path).replace("\\", "/").lstrip("/")
        fn = clean_filename(rel)
        if (self.res_dir / rel).is_file():
            return True, fn
        if fn and fn.lower() in self.extra_media:
            return True, fn
        return False, fn

    # ---------- 单条消息 -> TimelineItem ----------
    def render(self, m: dict) -> TimelineItem | None:
        mtype = m.get("type") or ""
        if m.get("recalled"):
            self.stats["dropped_recalled"] += 1
            return None
        if mtype in DROP_TYPES and not self.keep_system:
            self.stats["dropped_system"] += 1
            return None

        content = m.get("content") or {}
        elements = content.get("elements") or []
        texts: list[str] = []
        media: list[tuple[str, str]] = []
        mention_names: list[str] = []
        has_real_text = False

        for el in elements:
            et = el.get("type")
            data = el.get("data") or {}
            if et == "text":
                t = collapse_ws(data.get("text") or "")
                if t:
                    texts.append(t)
                    has_real_text = True
            elif et in ("image", "file"):
                fn = clean_filename(data.get("filename") or data.get("localPath") or data.get("url"))
                local_path = data.get("localPath") or ""
                ok, fn2 = self.media_exists(local_path)
                if not local_path and not self.export_has_media_files:
                    # R44（2026-10-08）：**整份导出都没带媒体文件** —— 元素上连 localPath 都没有，
                    # 说明这次导出根本没下载图片（不是"你删了图"）。此时媒体维度不适用：
                    # 按 R42「中间两步没导出图片文件时可以忽略」，不登记媒体引用，
                    # 只记数（原始 image/file 元素数），也不触发 ③删图对账 / ④OCR。
                    # 判据必须是"整份导出都没有 localPath"，因为混着下载了一部分时，
                    # 缺 localPath 的那几个仍按 R37 照留（那才真的是"文件不在这"）。
                    self.stats["media_unexported"] += 1
                else:
                    if fn:
                        self.referenced_media.add(fn.lower())
                    # R37（2026-09-20）：**默认保留所有媒体引用**。
                    # 文件在不在，只记数，不再因此丢消息 —— "这张图算不算有用"由你删文件 +
                    # `archive.py prune-missing` 决定，程序不替你判（旧行为是文件不在就丢整条）。
                    if ok:
                        self.stats["media_ok"] += 1
                    else:
                        self.stats["media_missing"] += 1
                    if fn2 or fn:
                        media.append(("image" if et == "image" else "file", fn2 or fn))
            elif et == "at":
                nm = data.get("name")
                if nm:
                    mention_names.append(nm)
                    texts.append("@" + self.label_of_name(nm))
                    has_real_text = True
            elif et == "reply":
                ref_id = str(data.get("referencedMessageId") or data.get("messageId") or "")
                ref = self._id_index.get(ref_id)
                if ref:
                    texts.append(f"↩{ref[0]}「{ref[1][:60]}」")
                else:
                    nm = data.get("senderName") or self._uid_to_name.get(str(data.get("senderUin") or ""), "")
                    rtx = collapse_ws(self._reply_text(data.get("previewElements")) or data.get("content") or "")
                    head = f"↩{self.label_of_name(nm)}" if nm else "↩"
                    texts.append(f"{head}「{rtx[:60]}」" if rtx else head)
            elif et == "face":
                nm = (data.get("name") or "").strip()
                texts.append(nm or "[表情]")
                has_real_text = True
            elif et == "market_face":
                pass  # 表情包（type_17）默认整体丢弃，见 has_real_text 判定
            elif et == "json":
                title = collapse_ws(data.get("title") or "")
                desc = collapse_ws(data.get("description") or "")
                if self.keep_json_share and (title or desc):
                    texts.append(f"[分享]{title}" + (f" — {desc}" if desc else ""))
                    has_real_text = True

        if not has_real_text and not media and mtype != "file":
            raw_text = collapse_ws(content.get("text") or "")
            t = re.sub(r"\[(图片|文件):[^\]]*\]", "", raw_text)
            t = re.sub(r"\[回复消息\]", "", t).strip()
            if t and t not in ("[JSON消息]",):
                texts.append(t)
                has_real_text = True

        if not has_real_text and not media:
            self.stats["dropped_empty"] += 1
            return None

        sd = m.get("sender") or {}
        name = sd.get("name") or "未知"
        text = collapse_ws(" ".join(texts))
        # 被回复内容里的 [图片:x]/[文件:x] 占位只说明"当时发过一张没留存的图"，
        # 文件名无意义且会让人误以为文件还在 -> 在引号内去掉，保留「有张图」这个信息
        text = re.sub(r"「[^「」]*」",
                      lambda mm: re.sub(r"\[图片:[^\]]*\]", "[图片]",
                                        re.sub(r"\[文件:[^\]]*\]", "[文件]", mm.group(0))),
                      text)
        hit = blacklist_hit(text, name, mention_names, self.blacklist)
        if hit:
            self.stats["dropped_blacklist"] += 1
            if len(self.blacklist_hits) < 50:
                self.blacklist_hits.append({"hit": hit, "who": name, "text": text[:90]})
            return None
        item = TimelineItem(
            ts=int(m.get("timestamp") or 0),
            time_local=fmt_local(m.get("timestamp")),
            sender_name=name,
            text=text[:MAX_TEXT_CHARS],
            media=media,
            mtype=mtype,
            mid=str(m.get("id") or ""),
        )
        return item

    @staticmethod
    def _reply_text(preview) -> str:
        """reply 的 previewElements 可能是 list / dict / str（不同导出器版本）。"""
        if not preview:
            return ""
        if isinstance(preview, str):
            return preview
        parts = []
        items = preview if isinstance(preview, list) else [preview]
        for p in items:
            if isinstance(p, dict):
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(x for x in parts if x)

    # ---------- 主流程 ----------
    def parse_timeline(self, existing_map: dict[str, str] | None = None) -> list[TimelineItem]:
        """解析出全部保留消息（时间升序），并把映射更新写回 self._name_seq。"""
        self.assign_name_map(existing_map)
        # 先建 id 索引，供回复引用（用字母，保持最终产出风格一致）
        for m in self.messages:
            mid = str(m.get("id") or "")
            content = m.get("content") or {}
            t = collapse_ws(content.get("text") or "")
            name = (m.get("sender") or {}).get("name") or ""
            if mid:
                self._id_index[mid] = (self.label_of_name(name), t)
        items = []
        for m in self.messages:
            it = self.render(m)
            if it:
                items.append(it)
                self.stats["kept"] += 1
        items.sort(key=lambda x: x.ts)
        return items

    @property
    def chat_name(self) -> str:
        return ((self.raw.get("chatInfo") or {}).get("name")) or self.src.stem

    @property
    def peer_uid(self) -> str:
        return str(((self.raw.get("chatInfo") or {}).get("peerUid")) or "")

    def name_map(self) -> dict[str, str]:
        return dict(self._name_seq)

    def media_warn(self) -> None:
        unexported = self.stats.get("media_unexported", 0)
        if unexported:
            # R44：整份导出没有 localPath = 这次导出没勾"导出媒体"，不是你把图删了。
            log(f"[媒体] 本次导出**没带媒体文件**：{unexported} 个 image/file 元素里没有一个带 "
                f"localPath（整份导出 {self.stats['total']} 条消息）")
            log("       按 R42「中间两步没导出图片文件时可以忽略」：**不登记媒体引用**，"
                "删图对账 / OCR 两步对这批自动跳过。")
            log("       想连图片本体一起归档 + 以后能 OCR，请在 QQChatExporter 里勾上导出媒体后重导。")
        miss, ok = self.stats["media_missing"], self.stats["media_ok"]
        if not miss:
            return
        total = miss + ok
        log(f"[媒体] 引用 {total} 个：本地有文件 {ok} 个 / 本地没有 {miss} 个（{miss / total:.0%} 缺失）")
        log("       缺失的**照留**（R37：只记数，不丢消息）。要按「文件被删」回删条目，"
            "自己挑范围跑 archive.py prune-missing")
        n_local = 0
        if self.res_dir.is_dir():
            for sub in ("images", "files"):
                p = self.res_dir / sub
                if p.is_dir():
                    n_local += sum(1 for f in p.iterdir() if f.is_file())
        if n_local == 0:
            # 实测踩过：把"导出没带图"误读成"群友删了图"
            log("[WARN] 这份导出的 resources/ 里一个文件都没有 —— **不是你把图删了，"
                "是这次导出没带媒体**（群里的图/表情没下载下来）")
            log("       影响：只带图片的消息会变成「只有文件名、没有文件」的条目"
                "（默认保留）；要连图片本体一起留，请在 QQChatExporter 里勾上导出媒体后重导")
        elif miss / total > 0.5:
            log("[WARN] 多数媒体本地不存在：可能是你筛选时删的，也可能是导出不完整 ——"
                " 程序分不出来，所以不替你做删除决定；用 prune-missing 按范围对账")


def fmt_local(ts_ms, tz_hours: int = 8) -> str:
    if not ts_ms:
        return ""
    t = int(ts_ms) / 1000 + tz_hours * 3600
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


# ══════════════════════════════ 一次性清洗（独立用法） ══════════════════════════════

def render_markdown(cleaner: Cleaner, items: list[TimelineItem], ocr: dict[str, str],
                    with_time: bool = False) -> str:
    st = cleaner.stats
    head = [
        f"# {cleaner.chat_name} · 精简聊天记录",
        "",
        f"- 来源导出：`{cleaner.src.name}`",
        f"- 原始消息 {st['total']} 条 → 保留 {st['kept']} 条（丢弃：系统 {st['dropped_system']} / "
        f"撤回 {st['dropped_recalled']} / 空与表情 {st['dropped_empty']} / "
        f"黑名单 {st.get('dropped_blacklist', 0)}）",
        f"- 媒体引用：{st['media_ok']} 个有本地文件"
        + (f"，{st['media_missing']} 个只有文件名（本地没有，按 R37 默认保留）"
           if st["media_missing"] else "")
        + (f"；本次导出没带媒体文件（{st.get('media_unexported', 0)} 个 image/file 元素无 "
           f"localPath，按 R44 不登记引用）" if st.get("media_unexported") else ""),
        "",
        "## 用户名代号映射",
        "",
        "| 代号 | 用户 |",
        "| --- | --- |",
    ]
    inv: dict[str, str] = {}
    for name, lab in cleaner.name_map().items():
        if Cleaner._is_real_name(name):
            inv.setdefault(lab, name)
    for lab in sorted(inv, key=lambda x: (len(x), x)):
        head.append(f"| {lab} | {inv[lab]} |")
    head += [
        "",
        "## 聊天记录",
        "",
        "> 格式：`代号: 文字 [图片:文件名] [文件:文件名]`；`↩` 后为被回复内容；`OCR:` 行为图片摘要。",
        "",
    ]
    lines: list[str] = []
    for it in items:
        marks = it.media_marks()
        seg = it.text if it.text else ""
        if marks:
            seg = f"{seg} {marks}".strip() if seg else marks
        if not seg:
            continue
        prefix = f"[{it.time_local}] " if with_time else ""
        lines.append(f"{prefix}{cleaner.label_of_name(it.sender_name)}: {seg}")
        for s in it.ocr:
            lines.append(f"    > OCR: {s}")
    return "\n".join(head + lines) + "\n"


def is_export_json(p: Path) -> bool:
    """是不是一份 QQChatExporter 导出（真正解析一遍，别靠猜）。

    只用"前 4KB 里有没有 messages 键"这种启发式会误判大文件：
    1 万条以上的导出，statistics 块就能把 messages 挤到 4KB 之外。
    """
    if p.name.endswith((".ocr_cache.json", ".clean.json")) or ".clean." in p.name:
        return False
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(raw, dict) and "messages" in raw and "chatInfo" in raw


def pick_default_input(d: str = r"C:\Users\HP\Documents\QQChatExporter\exports") -> Path:
    """取目录下最新的导出 json（排除清理产物 / OCR 缓存 / 非导出 json）。"""
    cands = [p for p in Path(d).glob("*.json") if is_export_json(p)]
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit(f"在 {d} 下没找到导出的 json")
    return cands[0]


def cmd_check(inp: Path) -> int:
    print("=== 环境体检 ===")
    print(f"输入文件     : {inp}  {'OK' if inp.is_file() else '缺失!'}")
    res = inp.parent / "resources"
    print(f"resources    : {res}  {'OK' if res.is_dir() else '缺失'}")
    for sub in ("images", "files"):
        d = res / sub
        n = len([f for f in d.iterdir() if f.is_file()]) if d.is_dir() else -1
        print(f"  - {sub:<6}: {n} 个文件")
    for label, mod in (("wechat_ocr", "wechat_ocr"), ("Pillow", "PIL")):
        try:
            m = __import__(mod)
            print(f"{label:<13}: OK {getattr(m, '__version__', '')}")
        except Exception as e:
            print(f"{label:<13}: 缺失 ({e})")
    print(f"WeChatOCR.exe: {WECHAT_OCR_EXE}  {'OK' if Path(WECHAT_OCR_EXE).is_file() else '缺失'}")
    print(f"WeChat 目录  : {WECHAT_DIR}  {'OK' if Path(WECHAT_DIR).is_dir() else '缺失'}")
    print(f"\nOCR 预算：由 `analyze.py ocr --limit N` 控制（硬上限 {OCR_HARD_MAX} 张）/ "
          f"超时 {OCR_TIMEOUT_SEC}s / 摘要 ≤{OCR_SUMMARY_CHARS} 字（OCR 只能手动、且必须限范围）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QQChatExporter 导出 JSON -> 精简聊天记录")
    ap.add_argument("-i", "--input", help="导出 JSON（默认取 exports 下最新）")
    ap.add_argument("-o", "--output", help="输出 md；'-' 表示 stdout")
    ap.add_argument("--ocr", action="store_true",
                    help="【已停用】OCR 只剩一条手动路径，见 analyze.py ocr")
    ap.add_argument("--keep-json-share", action="store_true", help="保留分享卡片文字")
    ap.add_argument("--keep-system", action="store_true", help="保留系统消息")
    ap.add_argument("--with-time", action="store_true", help="行首带本地时间戳")
    ap.add_argument("--check", action="store_true", help="只做环境体检")
    args = ap.parse_args(argv)

    inp = (Path(args.input).expanduser() if args.input else pick_default_input()).resolve()
    if not inp.is_file():
        raise SystemExit(f"输入不存在: {inp}")
    if args.check:
        return cmd_check(inp)

    if args.ocr:
        # R39/R40：OCR 只能手动、必须限范围，所以入口收敛成 analyze.py 一条。
        # 这个一次性清洗工具是"解析聊天记录"，不允许在解析时顺手 OCR。
        raise SystemExit(
            "这里不再做 OCR（R39/R40：解析聊天记录时不允许触发 OCR，OCR 必须手动且限定范围）。\n"
            "  正确用法：先把导出 import 进档案，再按范围识别：\n"
            "    python archive.py import --from <导出目录>\n"
            "    python analyze.py ocr --group <群名> --batch 2-4      # 或 --since/--until\n"
            "  只想要一份干净的 md（不 OCR）：把 --ocr 去掉重跑。")

    cleaner = Cleaner(inp, keep_json_share=args.keep_json_share, keep_system=args.keep_system)
    items = cleaner.parse_timeline()
    log(f"[解析] 保留 {cleaner.stats['kept']}/{cleaner.stats['total']} 条")
    cleaner.media_warn()

    out = render_markdown(cleaner, items, {}, with_time=args.with_time)
    if args.output == "-":
        force_utf8_console()
        print(out)
    else:
        outp = (Path(args.output).expanduser() if args.output
                else inp.parent / (inp.stem + ".clean.md")).resolve()
        outp.write_text(out, encoding="utf-8")
        log(f"[OK] 写出 {outp}（{len(out)} 字符）")
    return 0


if __name__ == "__main__":
    force_utf8_console()
    raise SystemExit(main())
