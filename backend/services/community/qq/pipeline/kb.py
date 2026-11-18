#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把某群「已提炼的批次」按**角度**合成一个多文件知识库，可反复追加。

分工
----
archive.py  记录事实：长期档案、时间轴去重、媒体归档。
analyze.py  逐批提炼：切批 → 调 LLM → 批次/batch-NNN.md（每批一份，互不重叠）。
kb.py       跨批合成：把批次结果按角度分文件归并，**每次只输出增量补丁**。

三个设计决定（都是踩出来的）
----------------------------
1. **一个角度一个文件**（`知识库/01-编程工具与IDE.md` …）。整份知识库塞一个文件会越来越难喂给模型：
   26 批的合成素材约 3 万字，每次让模型"把整份知识库重写一遍"既贵又容易悄悄丢条目。
2. **增量补丁而不是全文重写**：提交给模型的是「当前角度清单（每角度：已有对象名 + 1~2 条例）」+ 新素材，
   模型回 `[+]` 新增 / `[=]` 并入已有对象 / `[~]` 推翻修正，**程序负责切开、插进对应文件**。
   于是"合并同类项"这件事仍然由模型判断，但"改哪个文件的哪一行"由程序确定性执行，不靠模型自觉。
3. **信息量大的片段不做摘要**：单条 ≥240 字（或 ≥6 行）的消息，程序逐字抓进 `知识库/原文留存.md`
   （不经模型改写）。摘要会丢细节，长文恰恰是最不该丢的那部分。

幂等与防混用（`提炼/_kb.json`）
-------------------------------
* 已入库批次不会重复进库：`add` 缺省只取"已提炼但未入库"的批次；
* 素材提示词、合成提示词都记 sha1；换了合成提示词要 `--rebuild`（清空角度文件、用已有批次结果重放补丁）；
* 指纹与记录不一致 → 拒绝执行，除非 `--force`。

模型输出之后程序兜四件事
------------------------
1. 只认 `### 角度` 下的 `- [+]/- [=]/- [~]` 行，别的（前言、总结、解释）一律丢掉；
2. `[=]`/`[~]` 按对象名找行，找不到就降级成新增（并报告）；
3. 核对每个素材条目的来源记号是否都在；缺的按相似度贴回最像的行，再不行原文补到「其他」文件——
   **零丢失**是可核对的（`kb.py check`）；
4. 报告多余记号（模型编的）、疑似重复行、以及残留的说话人代号。

产出
----
  <群>/提炼/知识库/_索引.md         取材说明 + 角度清单 + 加工链（程序生成）
  <群>/提炼/知识库/NN-<角度>.md      每个角度一个文件
  <群>/提炼/知识库/原文留存.md       长文原文（程序从 消息.tsv 抓，不经模型）
  <群>/提炼/_kb.json                台账（批次、角度、指纹、时间）
  <群>/提炼/知识库历史/              每次改动前的快照 + 提交给模型的原始输入 + 改动记录

用法
----
  python kb.py init  --group "AI Agent 综合交流" --prompt prompts/kb_merge_aiagent.md
  python kb.py add   --group "AI Agent 综合交流"          # 自动追加所有未入库批次
  python kb.py add   --group "..." --rebuild              # 换提示词后清空重放
  python kb.py check --group "..."                        # 只核对，不调模型
  python kb.py quotes --group "..."                       # 只重抓原文留存
  python kb.py status
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive as A  # noqa: E402
import clean_json as cj  # noqa: E402
import llm_client as llm  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
_PARENTS = CODE_DIR.parents
_PROJECT = _PARENTS[4] if len(_PARENTS) > 4 and _PARENTS[4].name == "infosystem" else CODE_DIR
_DEFAULT_DATA = _PROJECT / "data" / "community" / "qq" / "pipeline"
ROOT = Path(os.environ.get("QQ_PIPELINE_DATA_DIR", str(_DEFAULT_DATA))).resolve()
CODE_ROOT = CODE_DIR
CONFIG_FP = CODE_DIR / "llm_config.json"
DEFAULT_PROMPT = "prompts/kb_merge_aiagent.md"
KB_DIR = "知识库"
# 知识库成品直接写进 knowledgebase/（2026-10-20 起）：以前写 data/.../提炼/知识库，
# 再由人工搬进 knowledgebase，重跑产线就会又落在 data。现在只有**提炼中间物**
# （批次/台账/历史）留 data，成品 md 直接落 knowledgebase/community/QQ/<群名>/。
KB_PUBLISH_ROOT = _PROJECT / "knowledgebase" / "community" / "QQ"
KB_INDEX = "_索引.md"


def kb_dir(group_dir: Path) -> Path:
    """群 → 知识库成品目录（knowledgebase 侧）。"""
    return KB_PUBLISH_ROOT / group_dir.name

STATE_NAME = "_kb.json"
HIST_DIR = "知识库历史"
BATCH_DIR = "批次"
QUOTES_NAME = "原文留存.md"

LONG_MSG_CHARS = 240      # 原文留存阈值：单条 ≥ 这么多字
LONG_MSG_LINES = 6        # 或换行 ≥ 这么多行
SALVAGE_SIM = 0.55        # 补记号时的相似度门槛
NEAR_DUP_SIM = 0.78       # 同文件内两条像到这个程度 → 报"疑似重复"
EXAMPLE_CLIP = 150        # 给模型看的例子里，单条截到多少字
MAX_OBJS_SHOWN = 12       # 给模型看的"已有对象"最多列几个

ITEM_RE = re.compile(r"^\[([A-Z])(\d+)\]\s*(.+?)\s*$")
RANGE_RE = re.compile(r"区间：(\S+ \S+)\s*~\s*(\S+ \S+)（(\d+)\s*条\s*/\s*(\d+)\s*字）")
DROP_RE = re.compile(r"省略\s*(\d+)\s*条")
MARK_RE = re.compile(r"批?\s?(\d{3})\s?([A-Z])\s?(\d+)")
CITE_ROW_RE = re.compile(r"#\s*(\d+)\s*(?:[-~—]\s*#?\s*(\d+))?")
MARK_GROUP_RE = re.compile(r"（[^（）]*?\d{3}\s?[KOA]\s?\d+[^（）]*?）")
SECTION_RE = re.compile(r"^##\s*[1-9][.、]")
PATCH_SEC_RE = re.compile(r"^#{2,4}\s*(.+?)\s*$")
PATCH_ITEM_RE = re.compile(r"^[-*]\s*\[([+=~])\]\s*(.+)$")
NEW_ANGLE_RE = re.compile(r"^(?:新增角度|新角度|NEW)\s*[：:]\s*(.+)$")
OBJ_RE = re.compile(r"\*\*(.+?)\*\*")
ANGLES_BLOCK_RE = re.compile(r"<!--\s*ANGLES\s*(.*?)-->", re.S)
ANGLE_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
def find_speaker_marks(text: str) -> list[str]:
    """找"说话人代号漏进正文"的痕迹（形如 `（A 说，C 附和）`、`（单人 G）`、`（Q、D 说）`）。

    整个括号里的内容必须**只由代号与「说/附和/反驳/…」组成**才算命中 ——
    早先用的是宽松正则（括号里只要出现"说"就算），结果把提示词自己要求的固定说法
    `（推销方说法，未获他人独立确认）`、`（两版说法）` 全误报成代号残留（实测 3 处误报）。
    """
    out: list[str] = []
    for m in re.finditer(r"[（(]([^（）()]{0,60})[）)]", text):
        inner = m.group(1)
        if SPEAKER_INNER_RE.match(inner):
            out.append(m.group(0))
    return out


CODE_HINT_RE = re.compile(r"[（(][^（）()]{0,60}?(?:说|附和|反驳|补充|讨论|报错)[^（）()]{0,60}?[）)]")
# 括号内容只由「代号 + 说/附和/…」构成才算代号残留（见 find_speaker_marks）
SPEAKER_INNER_RE = re.compile(
    r"^\s*(?:单人|多人)?\s*[A-Z]{1,3}"
    r"(?:\s*(?:[、,，]\s*[A-Z]{1,3}|\s*(?:说|附和|反驳|补充|讨论|报错|表示|认为)))*\s*$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def rel_of(fp: Path) -> str:
    try:
        return fp.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return fp.as_posix()


def prompt_meta(fp: Path) -> dict:
    t = fp.read_text(encoding="utf-8")
    return {"file": rel_of(fp), "name": fp.name, "sha1": fingerprint(t), "chars": len(t)}


def parse_idxs(s: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", (s or "").strip()):
        m = re.search(r"(\d+)", part)
        if m:
            out.append(int(m.group(1)))
    return sorted(set(out))


def batch_files(out: Path) -> list[int]:
    got = []
    for fp in (out / BATCH_DIR).glob("batch-*.md"):
        m = re.match(r"batch-(\d+)\.md$", fp.name)
        if m:
            got.append(int(m.group(1)))
    return sorted(set(got))


# ══════════════════════════════ 角度清单 ══════════════════════════════

def safe_name(name: str) -> str:
    s = name.replace(" · ", "-").replace("·", "-").replace(" ", "")
    s = re.sub(r"[\\/:*?\"<>|]", "", s)
    return s.strip("-")


def parse_angles(prompt_text: str) -> list[dict]:
    """角度清单的唯一真源是合成提示词里的 `<!-- ANGLES ... -->` 块（手改提示词即可增删角度）。"""
    m = ANGLES_BLOCK_RE.search(prompt_text)
    if not m:
        raise SystemExit("提示词里没有 `<!-- ANGLES ... -->` 角度清单块，无法建库。")
    out: list[dict] = []
    for line in m.group(1).splitlines():
        mm = ANGLE_LINE_RE.match(line)
        if not mm:
            continue
        no, name = int(mm.group(1)), mm.group(2).strip()
        out.append({"no": no, "name": name, "file": f"{no:02d}-{safe_name(name)}.md"})
    if not out:
        raise SystemExit("角度清单块是空的。")
    return out


def angle_by_name(manifest: list[dict], name: str) -> dict | None:
    n = re.sub(r"[\s·\-_]", "", name).lower()
    for a in manifest:
        if re.sub(r"[\s·\-_]", "", a["name"]).lower() == n:
            return a
    for a in manifest:                      # 宽松：包含关系
        an = re.sub(r"[\s·\-_]", "", a["name"]).lower()
        if an and (an in n or n in an):
            return a
    return None


def main_angle(manifest: list[dict]) -> dict:
    """兜底归属的角度（「其他」优先，没有就用最后一个）。"""
    for a in manifest:
        if "其他" in a["name"]:
            return a
    return manifest[-1]


# ══════════════════════════════ 解析批次结果 ══════════════════════════════

def parse_batch(out: Path, idx: int) -> dict:
    fp = out / BATCH_DIR / f"batch-{idx:03d}.md"
    if not fp.is_file():
        raise SystemExit(f"缺少批次结果：{fp}")
    items: list[dict] = []
    cites: list[dict] = []
    rng = ["", ""]
    n_msgs = n_chars = dropped = 0
    for raw in fp.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        m = ITEM_RE.match(line)
        if m:
            it = {"id": f"{idx:03d}{m.group(1)}{m.group(2)}",
                  "kind": m.group(1), "text": m.group(3)}
            # [Q#] 是"模型指认要逐字留存"的指针，不进角度文件，交给原文留存处理
            (cites if m.group(1) == "Q" else items).append(it)
            continue
        mm = RANGE_RE.search(line)
        if mm:
            rng = [mm.group(1), mm.group(2)]
            n_msgs, n_chars = int(mm.group(3)), int(mm.group(4))
            continue
        d = DROP_RE.search(line)
        if d:
            dropped = int(d.group(1))
    # 正文被裁过的话，未截断全文也读进来（信息不许凭空消失）
    full = out / BATCH_DIR / f"batch-{idx:03d}.full.md"
    extra: list[dict] = []
    if dropped and full.is_file():
        for line in full.read_text(encoding="utf-8").splitlines():
            m = ITEM_RE.match(line.rstrip())
            if not m or f"{idx:03d}{m.group(1)}{m.group(2)}" in {i["id"] for i in items + cites}:
                continue
            it = {"id": f"{idx:03d}{m.group(1)}{m.group(2)}", "kind": m.group(1),
                  "text": m.group(3), "from_full": True}
            (cites if m.group(1) == "Q" else items).append(it)
    return {"idx": idx, "file": fp.name, "range": rng, "n_msgs": n_msgs,
            "n_chars": n_chars, "dropped": dropped, "items": items, "cites": cites,
            "sha1": batch_sha(out, idx)}


def batch_sha(out: Path, idx: int) -> str:
    """批次结果文件的内容指纹（正文 + 未截断全文），用来判断"同号批次换了内容"。"""
    h = hashlib.sha1()
    for name in (f"batch-{idx:03d}.md", f"batch-{idx:03d}.full.md"):
        fp = out / BATCH_DIR / name
        if fp.is_file():
            h.update(fp.read_bytes())
    return h.hexdigest()[:12]


def source_prompt_of(out: Path, idxs: list[int]) -> dict:
    st = A.load_json(out / "_state.json", {})
    seen: dict[str, dict] = {}
    for i in idxs:
        p = (st.get("done", {}).get(str(i)) or {}).get("prompt")
        if isinstance(p, dict) and p.get("sha1"):
            seen[p["sha1"]] = p
    if len(seen) == 1:
        return next(iter(seen.values()))
    if len(seen) > 1:
        return {"mixed": sorted(seen.values(), key=lambda x: x.get("sha1", ""))}
    return {}


# ══════════════════════════════ 角度文件读写 ══════════════════════════════

def angle_path(kbdir: Path, a: dict) -> Path:
    return kbdir / a["file"]


def read_bullets(fp: Path) -> list[str]:
    if not fp.is_file():
        return []
    return [l.rstrip() for l in fp.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]


def dedupe_inline_marks(bullets: list[str]) -> tuple[list[str], int]:
    """同一行里同一个记号写了两次（模型把一条素材拆成两句话）→ 只留第一次。

    （v1 里有这个函数，v2 重写时弄丢了，实测又出现 `（022A1、022A1、026A1）`。）
    """
    out: list[str] = []
    n = 0
    for line in bullets:
        seen: set[str] = set()
        removed = 0

        def _repl(m: re.Match) -> str:
            nonlocal n, removed
            i = "".join(m.groups())
            if i in seen:
                n += 1
                removed += 1
                return ""
            seen.add(i)
            return m.group(0)

        s = MARK_RE.sub(_repl, line)
        if removed:                      # 只在真删过记号的行走一遍括号清理（不动别的正文）
            s = re.sub(r"（\s*[、，,]\s*", "（", s)
            s = re.sub(r"[、，,]\s*）", "）", s)
            s = re.sub(r"([、，,])\1+", r"\1", s)
            s = s.replace("（）", "")
        out.append(s)
    return out, n


def write_angle(fp: Path, a: dict, bullets: list[str], head_lines: list[str]) -> int:
    bullets, n = dedupe_inline_marks(bullets)
    body = "\n".join(bullets).rstrip()
    text = f"# {a['name']}\n\n" + "\n".join(head_lines) + "\n\n" + (body + "\n" if body else "")
    fp.write_text(text, encoding="utf-8")
    return n


def object_key(line: str) -> str:
    """条目的合并键 = `**对象名**`（去掉空白/·/-，小写）。"""
    m = OBJ_RE.search(line)
    if not m:
        return ""
    return re.sub(r"[\s·\-]", "", m.group(1)).lower()


def object_raw(line: str) -> str:
    m = OBJ_RE.search(line)
    return m.group(1).strip() if m else ""


def object_base(line: str) -> str:
    """对象名的第一个词（`DeepSeek 编程能力` → `DeepSeek`），只用于 check 里提示同类条目。"""
    return re.split(r"[\s·\-—]+", object_raw(line))[0].lower()


def find_by_key(bullets: list[str], key: str) -> int | None:
    if not key:
        return None
    for i, l in enumerate(bullets):
        if object_key(l) == key:
            return i
    return None


def resolve_target(bullets: list[str], key: str, raw: str = "") -> tuple[int | None, str]:
    """决定新条目该并进哪一条已有条目（返回 行号, 命中原因）。顺序：

    1. `exact`  同名对象
    2. `prefix` 已有对象名是新名字的前缀（Codex ← Codex 额度消耗）
    3. `back`   新名字是已有对象名的前缀，且唯一（DeepSeek ← DeepSeek 编程能力）
    4. `base`   主体名相同（Codex 上下文窗口 / Codex 20x 档 / Codex 降智 … 都归到第一条 Codex）

    模型经常给同一对象的新侧面起个带后缀的名字，靠 2~4 把它并回原条目，
    否则同一对象会越攒越多条（实测：Codex 攒成 5 条、Trae 出现两条）。
    """
    i = find_by_key(bullets, key)
    if i is not None:
        return i, "exact"
    if len(key) >= 3:
        cands = sorted(((len(object_key(l)), j) for j, l in enumerate(bullets)
                        if len(object_key(l)) >= 3 and key.startswith(object_key(l))),
                       reverse=True)
        if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
            return cands[0][1], "prefix"
    if len(key) >= 4:
        back = [j for j, l in enumerate(bullets)
                if len(object_key(l)) > len(key) and object_key(l).startswith(key)]
        if len(back) == 1:
            return back[0], "back-prefix"      # back 里存的就是行号（曾经写成 back[0][0] 崩过）
    base = re.split(r"[\s·\-—]+", raw.strip())[0].lower() if raw.strip() else ""
    if len(base) >= 3:
        same = [j for j, l in enumerate(bullets) if object_base(l) == base]
        if same:
            return same[0], "base"
    return None, ""


def find_marks(text: str, strict: bool = False) -> list[str]:
    """取来源记号。strict=True 只认写在括号里的（`（022K3、024K5）`）——那是我方约定的形状。

    为什么要 strict：图片文件名是 32 位十六进制（`…FF916F3D181B97.png`），
    宽松匹配会把 `916F3`、`181B97` 当成"记号"（实测误报过 3 个假孤儿）。
    """
    out: list[str] = []
    chunks = [m.group(1) for m in re.finditer(r"[（(]([^（）()]{0,140}?)[）)]", text)] if strict else [text]
    for ch in chunks:
        for m in MARK_RE.finditer(ch):
            i = "".join(m.groups())
            if i not in out:
                out.append(i)
    return out


def attach_marker(line: str, iid: str) -> str:
    hits = [m for m in MARK_GROUP_RE.finditer(line)]
    if hits:
        m = hits[-1]
        return line[:m.end() - 1] + f"、{iid}" + line[m.end() - 1:]
    return line.rstrip() + f"（{iid}）"


def strip_marks(text: str) -> str:
    s = MARK_GROUP_RE.sub("", text)
    return re.sub(r"\s+", " ", s).strip()


def merge_line(old: str, new: str) -> str:
    """[=]：把新细节接在旧条目末尾（去掉新行重复的 `**对象**：` 前缀与它自带的记号，稍后统一并入）。"""
    new_marks = find_marks(new, strict=True)
    body = strip_marks(re.sub(r"^\s*[-*]\s*\[=\]\s*", "", new))
    body = OBJ_RE.sub("", body, count=1).lstrip("：: ，,。;；").strip()
    merged = old.rstrip()
    if merged.endswith("。"):
        merged = merged[:-1]
    merged = f"{merged}；{body}"
    for iid in new_marks:
        merged = attach_marker(merged, iid)
    return merged


def replace_line(old: str, new: str) -> str:
    """[~]：用新说法替换旧说法，**旧来源记号 + 新来源记号全部保留**（来源链不能断）。

    两个坑都踩过：
      * 早期只保留旧记号 → 新素材的记号悬空，被"补录"机制当成丢失又写了一遍；
      * 返回时忘掉 `- ` 前缀 → 该行下次 read_bullets 时被当成非条目丢掉，条目凭空消失。
    """
    keep: list[str] = []
    for iid in find_marks(old, strict=True) + find_marks(new, strict=True):
        if iid not in keep:
            keep.append(iid)
    body = strip_marks(re.sub(r"^\s*[-*]\s*\[~\]\s*", "", new))
    for iid in keep:
        body = attach_marker(body, iid)
    return body if body.startswith("- ") else "- " + body.lstrip("- ").lstrip()


# ══════════════════════════════ 原文留存（不经过模型） ══════════════════════════════

def build_quotes(gd: Path, kbdir: Path, cites: list[dict] | None = None,
                 write: bool = True) -> dict:
    """原文留存：① 程序确定性抓（长消息）② 模型指认的按行号逐字抄回来。

    模型只负责"指认行号"，正文由程序从 消息.tsv 抄，所以不可能抄错、也不可能编内容。
    """
    rows = A.load_index(gd / "消息.tsv")
    picked: dict[int, dict] = {}
    for i, r in enumerate(rows, 1):
        t = r.get("text") or ""
        if len(t) >= LONG_MSG_CHARS or t.count("\n") >= LONG_MSG_LINES:
            picked[i] = {"row": i, "rec": r, "why": f"长文 ≥{LONG_MSG_CHARS}字"}
    cited, bad, cited_rows = 0, [], []
    for c in cites or []:
        want: list[int] = []
        for m in CITE_ROW_RE.finditer(c["text"]):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            if b < a:
                a, b = b, a
            want += list(range(a, min(b, a + 50 - 1) + 1))
        why = re.sub(r"#\s*\d+\s*(?:[-~—]\s*#?\s*\d+)?", "", c["text"])
        why = re.sub(r"^\s*留存\s*[：:｜|]?\s*", "", why).strip(" |｜：:")[:40] or "模型指认"
        for i in want:
            if 1 <= i <= len(rows):
                if i not in picked:
                    picked[i] = {"row": i, "rec": rows[i - 1], "why": f"{why}（{c['id']}）"}
                    cited += 1
                    cited_rows.append({"row": i, "why": why, "id": c["id"]})
                else:
                    cited_rows.append({"row": i, "why": f"{why}（已在自动抓取里）", "id": c["id"]})
            else:
                bad.append(f"{c['id']}→#{i}")
    q = {"n": len(picked), "chars": sum(len((p["rec"].get("text") or "")) for p in picked.values()),
         "cited": cited, "bad_cites": bad, "cited_rows": cited_rows}
    if write:
        L = [
            "# 原文留存 · 信息量大的片段（逐字保留，未经模型改写）",
            "",
            f"> 两种来源：① 程序自动抓（单条正文 ≥{LONG_MSG_CHARS} 字或换行 ≥{LONG_MSG_LINES} 行）；",
            f"> ② 提炼模型指认（`[Q#] 留存 #行号 | 为什么`）——**模型只给行号，正文由程序从 "
            f"`../消息.tsv` 抄**，不会抄错、不会编。",
            f"> `行#k` = 该文件的第 k 条数据（导入只往后追加，行号稳定）。",
            f"> 共 {len(picked)} 条 / {q['chars']} 字（其中模型指认新增 {cited} 条）。",
            "",
            "摘要会丢细节，长文恰恰是最不该丢的那部分，所以这里原样保留；",
            "角度文件里只放结论，需要原文时按 `行#k` 到这里查。",
            "",
        ]
        for i in sorted(picked):
            r = picked[i]["rec"]
            t = (r.get("text") or "").strip()
            L += [f"## 行#{i} · {r.get('time', '')} · {r.get('label', '')} · {len(t)} 字"
                  f" · {picked[i]['why']}", "", t, ""]
        (kbdir / QUOTES_NAME).write_text("\n".join(L), encoding="utf-8")
    return q


# ══════════════════════════════ 提交给模型的补丁输入 ══════════════════════════════

def angle_state(kbdir: Path, a: dict) -> tuple[list[str], list[str]]:
    bullets = read_bullets(angle_path(kbdir, a))
    objs = [OBJ_RE.search(l).group(1) for l in bullets if OBJ_RE.search(l)]
    seen, uniq = set(), []
    for o in objs:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq, bullets[:2]


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def build_patch_input(kbdir: Path, manifest: list[dict], chunk: list[dict]) -> str:
    L = [f"## 当前角度清单（共 {len(manifest)} 个；把每条新素材归到其中一个）", ""]
    for a in manifest:
        objs, exs = angle_state(kbdir, a)
        L.append(f"### {a['no']:02d} {a['name']}")
        if objs:
            more = f"…等{len(objs)}个" if len(objs) > MAX_OBJS_SHOWN else ""
            L.append("已有对象：" + "｜".join(objs[:MAX_OBJS_SHOWN]) + more)
        for e in exs:
            L.append("例：" + clip(e, EXAMPLE_CLIP))
        if not objs and not exs:
            L.append("（这个角度还空着）")
        L.append("")
    L += ["## 新增素材条目（每条末尾的来源记号，输出时必须原样带上）", ""]
    for b in chunk:
        rng = " ~ ".join(x for x in b["range"] if x)
        L.append(f"### batch-{b['idx']:03d}（{rng}）")
        for it in b["items"]:
            L.append(f"- {it['text']}（{it['id']}）")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def parse_patches(text: str, manifest: list[dict]) -> tuple[list[dict], list[str]]:
    """把模型输出切成 (角度, 符号, 正文) 三元组；不是补丁的行一律丢掉。"""
    patches: list[dict] = []
    new_angles: list[str] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = PATCH_ITEM_RE.match(line)
        if m and cur is not None:
            patches.append({"angle": cur, "mode": m.group(1), "text": m.group(2).strip()})
            continue
        if line.startswith("#"):
            name = PATCH_SEC_RE.match(line).group(1)
            na = NEW_ANGLE_RE.match(re.sub(r"^#+\s*", "", name))
            if na:
                nm = na.group(1).strip()
                if nm not in new_angles:
                    new_angles.append(nm)
                cur = {"new": nm}
            else:
                a = angle_by_name(manifest, name)
                cur = a if a else None
    return patches, new_angles


# ══════════════════════════════ 落盘 + 校验 ══════════════════════════════

def scan_marks(kbdir: Path, strict: bool = False) -> dict[str, list[str]]:
    """扫角度文件（`NN-*.md`）里的记号 → {记号: [出现的文件名]}。不扫索引与原文留存。"""
    seen: dict[str, list[str]] = {}
    for fp in sorted(kbdir.glob("[0-9][0-9]-*.md")):
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.startswith(">"):
                continue
            for i in find_marks(line, strict=strict):
                seen.setdefault(i, []).append(fp.name)
    return seen


def object_report(bullets: list[str]) -> dict:
    """同文件内的对象层面体检：同名重复 / 同一对象拆成好几条 / 单条过长。"""
    keys: dict[str, int] = {}
    bases: dict[str, list[str]] = {}
    longs = []
    for l in bullets:
        k = object_key(l)
        if k:
            keys[k] = keys.get(k, 0) + 1
        b = object_base(l)
        if len(b) >= 3:
            bases.setdefault(b, []).append(object_raw(l))
        if len(l) > 600:
            longs.append(l)
    return {
        "dup_keys": sorted(k for k, n in keys.items() if n > 1),
        "bases": {b: names for b, names in bases.items() if len(set(names)) > 1},
        "longs": longs,
    }


def near_dups(bullets: list[str]) -> list[tuple[str, str, float]]:
    out = []
    for i in range(len(bullets)):
        for j in range(i + 1, len(bullets)):
            a, b = strip_marks(bullets[i]), strip_marks(bullets[j])
            if len(a) < 20 or len(b) < 20:
                continue
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r >= NEAR_DUP_SIM:
                out.append((bullets[i], bullets[j], round(r, 2)))
    return out


def verify_and_repair(kbdir: Path, manifest: list[dict], items: dict[str, dict]) -> dict:
    marks = scan_marks(kbdir)                 # 宽松：只要有记号就算"没丢"
    strict = scan_marks(kbdir, strict=True)   # 严格：只认括号里的，用来判断"模型编的"
    missing = [i for i in items if i not in marks]
    orphans = sorted([i for i in strict if i not in items])
    cross = sorted([i for i, fs in strict.items() if len(set(fs)) > 1])
    attached, appended = [], []
    if missing:
        fallback = main_angle(manifest)
        for iid in missing:
            target = strip_marks(items[iid]["text"])
            best, best_fp, best_line = 0.0, None, None
            pool = [(fp, l) for fp in sorted(kbdir.glob("[0-9][0-9]-*.md"))
                    for l in read_bullets(fp)]
            for fp, l in pool:
                r = difflib.SequenceMatcher(None, target, strip_marks(l)).ratio()
                if r > best:
                    best, best_fp, best_line = r, fp, l
            if best_fp is not None and best >= SALVAGE_SIM:
                bullets = read_bullets(best_fp)
                k = bullets.index(best_line)
                bullets[k] = attach_marker(bullets[k], iid)
                a = next(x for x in manifest if x["file"] == best_fp.name)
                write_angle(best_fp, a, bullets, ["> （头部由 kb.py 重算）"])
                attached.append({"id": iid, "sim": round(best, 2), "file": best_fp.name})
            else:
                bullets = read_bullets(angle_path(kbdir, fallback))
                # 兜底补录的是批次层原文，可能带 `（A 说，C 附和）` 这类说话人代号；
                # 知识库正文不许有（实测漏过一次 `（Q、D 说）`），所以先剥掉再写。
                txt = items[iid]["text"]
                for hit in find_speaker_marks(txt):
                    txt = txt.replace(hit, "").strip()
                bullets.append(f"- {txt}（{iid}）")
                write_angle(angle_path(kbdir, fallback), fallback, bullets, ["> （头部由 kb.py 重算）"])
                appended.append(iid)
    return {"missing": missing, "orphans": orphans, "cross": cross,
            "attached": attached, "appended": appended}


# ══════════════════════════════ 渲染 ══════════════════════════════

def render_index(gd: Path, kbdir: Path, st: dict, manifest: list[dict],
                 parsed: dict[int, dict], sp: dict, pm: dict, body_chars: dict) -> str:
    meta = A.load_json(gd / "_meta.json", {})
    rec = st.get("batches", {})
    idxs = sorted(parsed)
    n_items = sum(len(parsed[i]["items"]) for i in idxs)
    rng = [b["range"] for b in parsed.values() if b["range"][0]]
    marks = scan_marks(kbdir)
    q = st.get("quotes") or {}
    L = [
        f"# {gd.name} · 知识库索引",
        "",
        "> 本文件由 `kb.py` 自动生成，**勿手改**（改了 `python kb.py check` 会发现对不上）。",
        "> 正文在 `NN-<角度>.md` 里，一个角度一个文件；`原文留存.md` 存长文原文。",
        "",
        f"- **来源群**：`{gd.name}`（群号 {meta.get('peer_uid', '?')}）；档案 `../消息.tsv`"
        f"（{meta.get('total', '?')} 条）",
        f"- **素材**＝已提炼批次：`批次/batch-{idxs[0]:03d}.md`"
        + (f" … `batch-{idxs[-1]:03d}.md`" if len(idxs) > 1 else "")
        + f"（共 {len(idxs)} 批 / {n_items} 条素材条目）",
        f"- **素材提示词**：{_fmt_src_prompt(sp)}",
        f"- **合成提示词**：`{pm.get('file', '?')}`（sha1 `{pm.get('sha1', '?')}`，"
        f"{pm.get('chars', '?')} 字；角度清单也在这个文件的 `<!-- ANGLES -->` 块里）",
        f"- **合成模型**：{st.get('model', '?')}；最后一次合成：{st.get('last_update', '?')}",
        f"- **覆盖区间**：{rng and min(r[0] for r in rng) or '?'} ~ {rng and max(r[1] for r in rng) or '?'}",
        f"- **来源记号**：`（022K3）` → 素材 `batch-022.md` 里的 `[K3]`。"
        f"`K`=技术结论 / `O`=图片·文件指引 / `A`=风险与广告；正文里**不写说话人代号**",
        f"- **原文留存**：`{QUOTES_NAME}`（{q.get('n', '?')} 条 / {q.get('chars', '?')} 字，"
        f"单条 ≥{LONG_MSG_CHARS} 字或 ≥{LONG_MSG_LINES} 行）",
        "",
        f"### 角度清单（共 {len(manifest)} 个）",
        "",
        "| # | 角度 | 文件 | 条数 | 字数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a in manifest:
        fp = angle_path(kbdir, a)
        n = body_chars.get(a["file"], (0, 0))
        L.append(f"| {a['no']:02d} | {a['name']} | `{a['file']}` | {n[0]} | {n[1]} |")
    L += [
        f"| — | **合计** | {len(marks)} 个来源记号 | {sum(v[0] for v in body_chars.values())} | "
        f"{sum(v[1] for v in body_chars.values())} |",
        "",
        "### 加工链（用来判断「这一批有没有进过库」）",
        "",
        "| 批次 | 区间 | 原始条数 | 素材条目 | 进库时间 | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i in idxs:
        b, r = parsed[i], rec.get(str(i), {})
        note = []
        if b["dropped"]:
            note.append(f"正文被 1200/上限裁过（省略 {b['dropped']} 条，未截断全文在 "
                        f"`批次/batch-{i:03d}.full.md`，已一并入库）")
        if any(it.get("from_full") for it in b["items"]):
            note.append("含 full 文件补录")
        L.append(f"| batch-{i:03d} | {b['range'][0]} ~ {b['range'][1]} | {b['n_msgs']} | "
                 f"{len(b['items'])} | {r.get('merged_at', '?')} | {'；'.join(note)} |")
    L += ["", "---", "", ""]
    rows_cited = (q or {}).get("cited_rows") or []
    if rows_cited:
        L += ["### 提炼模型指认的原文（逐字在 `原文留存.md` 里）", ""]
        for r in rows_cited:
            L.append(f"- `行#{r['row']}` · {r['why']}（{r['id']}）")
        L += [""]
    L += [
        f"> 其余 `{QUOTES_NAME}` 里的条目是程序按「单条 ≥{LONG_MSG_CHARS} 字或 ≥{LONG_MSG_LINES} 行」自动抓的；",
        "> 上面这些是提炼模型另外指认、程序按行号逐字抄回来的。",
        "",
    ]
    return "\n".join(L)


def _fmt_src_prompt(sp: dict) -> str:
    if sp.get("mixed"):
        return "；".join(f"`{p.get('file', '?')}`（sha1 `{p.get('sha1', '?')}`）" for p in sp["mixed"]) \
            + " ← **混了两代提示词，建议 `add --rebuild` 统一**"
    if sp.get("file"):
        return f"`{sp['file']}`（sha1 `{sp.get('sha1', '?')}`，{sp.get('chars', '?')} 字）"
    return "（台账里没记录；用 `python analyze.py stamp --group ... --prompt ...` 补记）"


def refresh_headers(gd: Path, kbdir: Path, st: dict, manifest: list[dict], parsed: dict[int, dict],
                    sp: dict, pm: dict) -> dict:
    """重算每个角度文件的头部与索引；返回 {文件名: (条数, 字数)}。"""
    rec = st.get("batches", {})
    idxs = sorted(rec and [int(k) for k in rec] or parsed)
    batches = "、".join(f"{i:03d}" for i in idxs)
    body_chars: dict[str, tuple[int, int]] = {}
    n_fixed = 0
    for a in manifest:
        fp = angle_path(kbdir, a)
        bullets = read_bullets(fp)
        body_chars[a["file"]] = (len(bullets), sum(len(b) for b in bullets))
        head = [
            f"> 取材：`{gd.name}` · 批次 {batches} · 素材提示词 "
            f"`{sp.get('name', '?')}#{sp.get('sha1', '?')}` · 合成提示词 "
            f"`{pm.get('name', '?')}#{pm.get('sha1', '?')}`",
            f"> {len(bullets)} 条 / {sum(len(b) for b in bullets)} 字"
            f" · 末尾 `（NNNK#）` 是来源记号 · `python kb.py check` 可核对",
        ]
        n_fixed += write_angle(fp, a, bullets, head)
    if n_fixed:
        log(f"[知识库] 顺带合并了同行重复的记号 {n_fixed} 个（正文其余部分没动）")
    (kbdir / KB_INDEX).write_text(
        render_index(gd, kbdir, st, manifest, parsed, sp, pm, body_chars), encoding="utf-8")
    return body_chars


def write_snapshot(kbdir: Path, hist: Path, tag: str) -> Path:
    hist.mkdir(parents=True, exist_ok=True)
    dst = hist / f"kb-{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
    dst.mkdir(parents=True, exist_ok=True)
    for fp in sorted(kbdir.glob("*.md")):
        (dst / fp.name).write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


# ══════════════════════════════ 主流程 ══════════════════════════════

def stamp_batches(st: dict, parsed: dict[int, dict], idxs: list[int], when: str) -> None:
    """把批次台账写进 _kb.json（每个 chunk 落一次盘，保证中途崩了也能接着跑）。"""
    for i in idxs:
        b = parsed[i]
        st.setdefault("batches", {}).setdefault(str(i), {}).update({
            "file": b["file"], "range": b["range"], "n_msgs": b["n_msgs"],
            "n_chars": b["n_chars"], "items": len(b["items"]), "dropped": b["dropped"],
            "sha1": b.get("sha1", ""), "merged_at": when})


def cmd_build(args) -> int:
    gd = ROOT / args.group
    out = gd / "提炼"
    if not out.is_dir():
        raise SystemExit(f"没有提炼目录：{out}")
    kbdir = kb_dir(gd)
    kbdir.mkdir(parents=True, exist_ok=True)
    hist = out / HIST_DIR
    state_fp = out / STATE_NAME
    st = A.load_json(state_fp, {"batches": {}, "history": []})
    rec: dict = st.get("batches", {})
    rebuild = (args.mode == "init") or args.rebuild

    if args.mode == "init" and (st.get("angles") or (kbdir / KB_INDEX).is_file()) and not args.force:
        raise SystemExit(f"知识库已存在。整篇重做请用 `add --rebuild`（会先快照旧版）。")

    want = parse_idxs(args.batches) if args.batches else batch_files(out)
    if not want:
        raise SystemExit("没找到任何批次结果（提炼/批次/batch-NNN.md）")
    rec_idx = {int(k) for k in rec}
    gone_rec = sorted(rec_idx - set(want))
    if gone_rec:
        # 台账里有、磁盘上已经没有批次文件 = 上一轮切法留下的幽灵条目（批次号重排后出现）。
        # 不能拿它去 parse_batch（会 SystemExit），也不能让它继续留在 final 里。
        log(f"[知识库] ⚠ 台账里有但磁盘上已无批次文件："
            f"{', '.join('batch-%03d' % i for i in gone_rec)} → 本次不含它们")
        rec_idx -= set(gone_rec)
    if rebuild:
        new = sorted(set(want) | rec_idx)
        final = list(new)
    else:
        # 「批次号见过」不等于「这批内容进过库」：补漏/重切之后同号批次会换内容
        # （实测 batch-019 从 35 条变 704 条），只看批次号会把新内容静默跳过，
        # 而台账又被 refresh 重写成"已入库 N 条"，连差异都看不出来。
        stale = []
        for i in want:
            rec_i = rec.get(str(i)) or {}
            if i in rec_idx and rec_i.get("sha1") and rec_i["sha1"] != batch_sha(out, i):
                stale.append(i)
        for i in stale:
            rec_i = rec.get(str(i)) or {}
            log(f"[知识库] batch-{i:03d} 内容已变（台账 {rec_i.get('n_msgs')} 条 → "
                f"现在 {parse_batch(out, i)['n_msgs']} 条）→ 重新入库")
        new = [i for i in want if i not in rec_idx or i in set(stale)]
        final = sorted(rec_idx | set(new))
    if not new:
        log(f"[知识库] {gd.name}：没有新批次可追加（已入库 {len(rec)} 批）。")
        return 0

    # ── 提示词与指纹 ──
    pf = Path(args.prompt) if args.prompt else (CODE_DIR / (st.get("prompt") or {}).get("file", DEFAULT_PROMPT))
    if not pf.is_absolute():
        pf = CODE_DIR / pf
    if not pf.is_file():
        raise SystemExit(f"合成提示词不存在：{pf}")
    pt = pf.read_text(encoding="utf-8")
    pm = prompt_meta(pf)
    manifest = parse_angles(pt)
    for old_a in (st.get("angles") or []):      # 模型上轮新增的角度要留着（提示词里没有）
        if not angle_by_name(manifest, old_a["name"]):
            manifest.append(old_a)
            log(f"[知识库] 沿用上轮新增的角度：{old_a['name']} → {old_a['file']}")
    old_pm = st.get("prompt") or {}
    if old_pm and old_pm.get("sha1") != pm["sha1"] and not rebuild:
        raise SystemExit(
            f"合成提示词与入库时不一致（记录 {old_pm.get('sha1')} → 现在 {pm['sha1']}）。\n"
            f"  · 想换提示词：`python kb.py add --group \"{args.group}\" --rebuild`\n"
            f"  · 想照旧：去掉 --prompt")
    sp = source_prompt_of(out, final) or (st.get("source_prompt") or {})
    old_sp = st.get("source_prompt") or {}
    if (not rebuild and old_sp.get("sha1") and sp.get("sha1") and old_sp["sha1"] != sp["sha1"]
            and not args.force):
        raise SystemExit(f"素材提示词和入库时不一致（记录 {old_sp.get('sha1')} → 现在 {sp.get('sha1')}）。"
                         f"\n  确认要混就加 --force。")

    parsed_all = {i: parse_batch(out, i) for i in final}
    chunk_all = [parsed_all[i] for i in new]
    kbdir.mkdir(parents=True, exist_ok=True)

    # ── 原文留存（确定性 + 模型指认；先把模型给的 [Q#] 行号收集起来）──
    cites = [c for b in parsed_all.values() for c in b.get("cites", [])]
    q = build_quotes(gd, kbdir, cites, write=not args.dry_run)
    log(f"[知识库] 原文留存：{q['n']} 条 / {q['chars']} 字"
        f"（≥{LONG_MSG_CHARS}字或≥{LONG_MSG_LINES}行自动抓；模型指认新增 {q['cited']} 条）")
    if q["bad_cites"]:
        log(f"[知识库] ⚠ 模型指认的行号不存在：{', '.join(q['bad_cites'])}")

    client = None if args.dry_run else llm.LLMClient(llm.load_config(CONFIG_FP, args.model))

    if rebuild and not args.dry_run:
        if (kbdir / KB_INDEX).is_file():
            sb = write_snapshot(kbdir, hist, "before-rebuild")
            log(f"[知识库] 旧版已快照：{HIST_DIR}/{sb.name}/")
        for a in manifest:                       # 清空角度文件，用已有批次结果重放补丁
            angle_path(kbdir, a).write_text("", encoding="utf-8")

    per = max(1, args.per_call)
    chunks = [chunk_all[i:i + per] for i in range(0, len(chunk_all), per)]
    log(f"[知识库] {gd.name}：{'重放' if rebuild else '追加'} {len(new)} 批 / "
        f"{sum(len(b['items']) for b in chunk_all)} 条，分 {len(chunks)} 次调用"
        f"（每次最多 {per} 批）；素材库共 {len(final)} 批")
    hist.mkdir(parents=True, exist_ok=True)

    stats = {"new": 0, "merge": 0, "replace": 0, "auto_merge": 0, "replace_downgraded": 0,
             "fallback_new": 0, "unknown_angle": 0}
    changes: list[str] = []
    for ci, chunk in enumerate(chunks, 1):
        user = build_patch_input(kbdir, manifest, chunk)
        tag = "rebuild" if rebuild else "add"
        inp = hist / (f"kb-input-{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{ci}"
                      + (".dryrun.txt" if args.dry_run else ".txt"))
        inp.write_text("### SYSTEM\n" + pt + "\n\n### USER\n" + user, encoding="utf-8")
        if args.dry_run:
            log(f"[dry-run] 第 {ci}/{len(chunks)} 次输入 {len(user)} 字 → {inp.name}")
            continue
        t0 = time.time()
        raw = client.ask(pt, user, max_tokens=args.max_tokens)
        patches, new_angles = parse_patches(raw, manifest)
        for nm in new_angles:                    # 模型声明的新角度 → 建文件并记入清单
            if angle_by_name(manifest, nm):
                continue
            no = max(a["no"] for a in manifest) + 1
            manifest.append({"no": no, "name": nm, "file": f"{no:02d}-{safe_name(nm)}.md"})
            (kbdir / manifest[-1]["file"]).write_text("", encoding="utf-8")
            log(f"[知识库] 新角度：{nm} → {manifest[-1]['file']}")
        for p in patches:
            a = p["angle"]
            if isinstance(a, dict) and a.get("new"):
                a = angle_by_name(manifest, a["new"]) or main_angle(manifest)
            if isinstance(a, dict) and a.get("file") is None:
                a = main_angle(manifest)
            if a is None:
                a = main_angle(manifest)
                stats["unknown_angle"] += 1
            fp = angle_path(kbdir, a)
            bullets = read_bullets(fp)
            key = object_key(p["text"])
            idx, why = resolve_target(bullets, key, object_raw(p["text"])) if key else (None, "")
            if p["mode"] == "~" and idx is not None:
                old = bullets[idx]
                cand = replace_line(old, p["text"])
                # 护栏：替换只允许发生在"信息量不减少"的前提下。新说法比旧说法短太多
                # 就说明模型把 [~] 当 [=] 用了（实测把 Trae 的一整条旧事实换没了），此时改成并入。
                if len(strip_marks(cand)) < 0.8 * len(strip_marks(old)):
                    stats["replace_downgraded"] += 1
                    cand = merge_line(old, p["text"])
                    changes.append(f"- [{now()}] `{a['file']}` ← [~]降级为[=]（新说法没覆盖旧事实）\n"
                                   f"  - 旧：{old}\n  - 现：{cand}")
                    log(f"[知识库] [~] 新说法比旧说法短（{len(strip_marks(cand))} < "
                        f"{int(0.8 * len(strip_marks(old)))}）→ 疑似丢信息，已改为并入："
                        f"{object_raw(old)}")
                else:
                    changes.append(f"- [{now()}] `{a['file']}` ← {why} [~]\n  - 旧：{old}\n  - 新：{cand}")
                    stats["replace"] += 1
                bullets[idx] = cand
            elif idx is not None:
                # `[=]` 本来就该并入；`[+]` 但对象名已存在（含前缀同名）→ 也是并入，
                # 不然同一对象会攒出一堆条目（程序说了算，不靠模型自觉）
                if p["mode"] == "+":
                    stats["auto_merge"] += 1
                    log(f"[知识库] 「{a['name']}」里已有同名对象（{why}），[+] 已自动并入："
                        f"{object_raw(p['text']) or object_key(p['text'])}")
                else:
                    stats["merge"] += 1
                bullets[idx] = merge_line(bullets[idx], p["text"])
            else:
                if p["mode"] in "=~":
                    stats["fallback_new"] += 1
                    log(f"[知识库] {p['mode']} 没找到对象「{object_raw(p['text'])}」→ 降级成新增")
                bullets.append(f"- {p['text']}")
                stats["new"] += 1
            write_angle(fp, a, bullets, ["> （头部由 kb.py 重算）"])
        log(f"[知识库] 第 {ci}/{len(chunks)} 次：{len(patches)} 条补丁"
            f"（新增 {stats['new']} / 并入 {stats['merge']} / 自动并入 {stats['auto_merge']}"
            f" / 替换 {stats['replace']} / 替换降级 {stats['replace_downgraded']}）"
            f"，{time.time() - t0:.1f}s")
        # 每个 chunk 落一次台账：中途崩了/断电了，重跑 add 只补剩下的批次（不会重复入库）
        st["angles"] = manifest
        st["prompt"], st["model"], st["quotes"] = pm, client.config.model, q
        st["last_update"] = now()
        stamp_batches(st, parsed_all, [b["idx"] for b in chunk], st["last_update"])
        A.save_json(state_fp, st)
    if args.dry_run:
        return 0

    items = {it["id"]: it for b in parsed_all.values() for it in b["items"]}
    rep = verify_and_repair(kbdir, manifest, items)
    if rep["appended"]:
        log(f"[知识库] ⚠ 模型完全没处理这些素材，已原文补录到「其他」：{', '.join(rep['appended'])}")
    if rep["attached"]:
        log("[知识库] 这些记号模型漏了，已按相似度贴回最像的行："
            + ", ".join(f"{x['id']}(相似度{x['sim']},{x['file']})" for x in rep["attached"]))
    if rep["orphans"]:
        log(f"[知识库] ⚠ 多余记号（模型编的或旧残留）：{', '.join(rep['orphans'])}")
    if rep["cross"]:
        log(f"[知识库] ⚠ 这些素材被写到多个文件里：{', '.join(rep['cross'])}（看一眼有没有重复）")
    if stats["fallback_new"]:
        log(f"[知识库] {stats['fallback_new']} 条 [=]/[~] 没找到同名对象 → 降级成新增（对象名没对齐）")
    if stats["unknown_angle"]:
        log(f"[知识库] {stats['unknown_angle']} 条补丁的角度名没匹配上 → 归到「其他」")

    when = now()
    st.update({"group": gd.name, "prompt": pm, "model": client.config.model,
               "source_prompt": sp, "last_update": when, "angles": manifest, "quotes": q})
    stamp_batches(st, parsed_all, final, when)
    body_chars = refresh_headers(gd, kbdir, st, manifest, parsed_all, sp, pm)
    # 台账收敛到"真实存在的批次"：幽灵条目（批次号重排后消失的）在这里除掉，
    # 否则 check 永远报"台账有、磁盘没批次文件"，且每次 add 都要重打一遍警告。
    for k in list((st.get("batches") or {})):
        if int(k) not in final:
            del st["batches"][k]
    if changes:
        (hist / "改动记录.md").write_text(
            f"# [~] 替换记录（{when}）\n\n" + "\n".join(changes) + "\n", encoding="utf-8")
        log(f"[知识库] {stats['replace']} 条 [~] 替换已记入 {HIST_DIR}/改动记录.md（可回退）")
    st.setdefault("history", []).append(
        {"at": when, "kind": "rebuild" if rebuild else "add", "added": new, "stats": stats})
    A.save_json(state_fp, st)
    marks = scan_marks(kbdir)
    items_all: dict[str, dict] = {}
    for i in (new or []):
        for it in parsed_all.get(int(i), {}).get("items", []):
            items_all[it["id"]] = it
    if not items_all:      # 需要时用入库批次的全量素材
        for k in (st.get("batches") or {}):
            for it in parse_batch(out, int(k))["items"]:
                items_all[it["id"]] = it
    log(f"[知识库] 完成：{sum(v[0] for v in body_chars.values())} 条 / "
        f"{sum(v[1] for v in body_chars.values())} 字"
        f"，分 {len(manifest)} 个角度文件 → {KB_DIR}\\")
    log(f"[知识库] {mark_summary(kbdir, items_all)}")
    return 0


def cmd_check(args) -> int:
    out = ROOT / args.group / "提炼"
    kbdir = kb_dir(ROOT / args.group)
    if not (kbdir / KB_INDEX).is_file():
        raise SystemExit(f"还没有知识库：{kbdir}")
    st = A.load_json(out / STATE_NAME, {})
    manifest = st.get("angles") or []
    rec = [int(k) for k in (st.get("batches") or {})]
    on_disk = batch_files(out)
    marks = scan_marks(kbdir)                 # 宽松：判定"素材有没有丢"
    strict = scan_marks(kbdir, strict=True)   # 严格：判定"库里有没有编出来的记号"
    parsed = {i: parse_batch(out, i) for i in rec} if rec else {}
    items = {it["id"]: it for b in parsed.values() for it in b["items"]}
    missing = sorted(i for i in items if i not in marks)
    orphans = sorted(i for i in strict if i not in items)
    cross = sorted(i for i, fs in scan_marks(kbdir, strict=True).items() if len(set(fs)) > 1)
    print(f"{ROOT / args.group}")
    total_b = sum(len(read_bullets(angle_path(kbdir, a))) for a in manifest)
    total_c = sum(len(l) for a in manifest for l in read_bullets(angle_path(kbdir, a)))
    q = st.get("quotes") or {}
    print(f"  知识库目录：{KB_DIR}\\（{len(manifest)} 个角度文件 / {total_b} 条 / {total_c} 字）")
    for a in manifest:
        bl = read_bullets(angle_path(kbdir, a))
        print(f"    {a['no']:02d} {a['name']:<18} {len(bl):>3} 条 / {sum(len(l) for l in bl):>5} 字")
    print(f"  原文留存：{QUOTES_NAME} {q.get('n', '?')} 条 / {q.get('chars', '?')} 字")
    print(f"  台账：{len(rec)} 批已入库 {rec}")
    if on_disk and (set(on_disk) - set(rec)):
        print(f"    · 未收录（可 add 追加）：{sorted(set(on_disk) - set(rec))}")
    if set(rec) - set(on_disk):
        print(f"    · 台账有、磁盘没批次文件：{sorted(set(rec) - set(on_disk))}")
    print(f"  素材条目：{len(items)} 条")
    stale = [f"batch-{i:03d}（台账 {st['batches'][str(i)].get('items')} 条 → 现在 {len(parsed[i]['items'])} 条）"
             for i in rec if (st.get("batches", {}).get(str(i), {}) or {}).get("items") is not None
             and st["batches"][str(i)]["items"] != len(parsed[i]["items"])]
    if stale:
        print("  ⚠ 批次文件被重新提炼过，知识库已过期：" + "，".join(stale)
              + f"\n    → `python kb.py add --group \"{args.group}\" --rebuild`")
    ok = True
    if missing:
        ok = False
        print(f"  ✘ 素材条目在知识库里找不到（丢了）：{', '.join(missing)}")
    if orphans:
        ok = False
        print(f"  ✘ 知识库里的记号不在素材台账（编的/旧残留）：{', '.join(orphans)}")
    if cross:
        print(f"  ⚠ 这些素材被写到多个文件：{', '.join(cross)}（可能是拆到两节，也可能重复）")
    n_dup = 0
    for a in manifest:
        bullets = read_bullets(angle_path(kbdir, a))
        rep = object_report(bullets)
        if rep["dup_keys"]:
            print(f"  ⚠ 「{a['name']}」里有同名对象写成多条：{', '.join(rep['dup_keys'])}")
        for b, names in rep["bases"].items():
            print(f"  ⚠ 「{a['name']}」里同一对象拆成 {len(set(names))} 条（{b}）：{' ｜ '.join(names)}")
        for l in rep["longs"]:
            print(f"  ⚠ 「{a['name']}」有 {len(l)} 字的超长条目（考虑拆开或精简）：{clip(strip_marks(l), 80)}")
        per: dict[str, int] = {}
        for l in bullets:
            for iid in find_marks(l, strict=True):
                per[iid] = per.get(iid, 0) + 1
        multi = sorted(k for k, n in per.items() if n > 1)
        if multi:
            print(f"  ⚠ 「{a['name']}」里这些素材被拆到多行：{', '.join(multi)}"
                  f"（一条素材讲了几件事时允许，但要确认两处内容没重复）")
        for x, y, r in near_dups(bullets):
            n_dup += 1
            print(f"  ⚠ 「{a['name']}」里两行很像（{r}）：\n      A: {clip(x, 90)}\n      B: {clip(y, 90)}")
    where: dict[str, list[str]] = {}
    for a in manifest:
        for l in read_bullets(angle_path(kbdir, a)):
            k = object_key(l)
            if len(k) >= 4:
                where.setdefault(k, [])
                if a["name"] not in where[k]:
                    where[k].append(a["name"])
    for k, files in sorted(where.items()):
        if len(files) > 1:
            print(f"  ⚠ 同一对象「{k}」出现在多个角度文件：{'、'.join(files)}（看看是否该只留一处）")
    bad_code = []
    for fp in sorted(kbdir.glob("[0-9][0-9]-*.md")):
        for hit in find_speaker_marks(fp.read_text(encoding="utf-8")):
            bad_code.append(f"{fp.name}: {hit}")
    if bad_code:
        print(f"  ⚠ 正文里还有说话人代号痕迹（要求是不写）：{'; '.join(bad_code[:6])}"
              + (f" 等 {len(bad_code)} 处" if len(bad_code) > 6 else ""))
    if ok:
        print("  ✔ 来源记号完全对齐（无丢失 / 无多余）")
    sp, pm = st.get("source_prompt") or {}, st.get("prompt") or {}
    print(f"  素材提示词：{_fmt_src_prompt(sp)}")
    print(f"  合成提示词：{_fmt_src_prompt(pm)}")
    return 0 if ok else 1


def cmd_quotes(args) -> int:
    gd = ROOT / args.group
    out = gd / "提炼"
    kbdir = kb_dir(gd)
    kbdir.mkdir(parents=True, exist_ok=True)
    st = A.load_json(out / STATE_NAME, {})
    cites: list[dict] = []
    for k in (st.get("batches") or {}):
        cites += parse_batch(out, int(k)).get("cites", [])
    q = build_quotes(gd, kbdir, cites)
    st["quotes"] = q
    A.save_json(out / STATE_NAME, st)
    log(f"[知识库] 原文留存已重抓：{q['n']} 条 / {q['chars']} 字"
        f"（模型指认 {q['cited']} 条）→ {KB_DIR}/{QUOTES_NAME}")
    return 0


def cmd_rehead(args) -> int:
    gd = ROOT / args.group
    out = gd / "提炼"
    st = A.load_json(out / STATE_NAME, {})
    manifest = st.get("angles") or []
    if not manifest:
        raise SystemExit("台账里没有角度清单，先 init/add")
    parsed = {int(i): parse_batch(out, int(i)) for i in (st.get("batches") or {})}
    bc = refresh_headers(gd, kb_dir(gd), st, manifest, parsed,
                         st.get("source_prompt") or {}, st.get("prompt") or {})
    log(f"[知识库] 头部已重算：{sum(v[0] for v in bc.values())} 条 / "
        f"{sum(v[1] for v in bc.values())} 字（正文没动）")
    return 0


def mark_summary(kbdir: Path, items: dict[str, dict]) -> str:
    """一句话说清"素材记号收全了没"。三种口径要分开讲，否则数字看起来对不上：

    - 素材条目数（批次里的 `[K#]`）—— 应该收进知识库的总数
    - **宽松**命中（记号出现在正文任何位置都算）—— 用来判"丢没丢"（宁可漏报孤儿）
    - **严格**命中（只认写在括号里的）—— 用来判"有没有编"（明文约定形状是 `（022K3）`）
    - 形似记号但不是素材的（图片文件名里的 32 位十六进制）—— 宽松口径的假阳性，要说明
    """
    loose, strict = scan_marks(kbdir), scan_marks(kbdir, strict=True)
    n = len(items)
    hit = sum(1 for i in items if i in loose)
    bare = sum(1 for i in items if i not in strict)
    phantom = sum(1 for i in loose if i not in items)
    s = f"素材记号 {hit}/{n} 已收录"
    if bare:
        s += f"（另有 {bare} 条的记号线夹在超长括号里，严格匹配认不出，**不是漏写**）"
    if phantom:
        s += f"；另有 {phantom} 处形似记号的字符串不是素材（多为图片文件名，忽略）"
    return s


def cmd_status(args) -> int:
    if not args.group:
        for gd in sorted(p for p in ROOT.iterdir() if (p / "_meta.json").is_file()):
            out = gd / "提炼"
            st = A.load_json(out / STATE_NAME, {})
            rec = st.get("batches") or {}
            done = len(A.load_json(out / "_state.json", {}).get("done", {}))
            disk = batch_files(out)
            print(f"{gd.name}")
            print(f"  已归档消息：{A.load_json(gd / '_meta.json', {}).get('total', '?')} 条")
            print(f"  已提炼批次：{done}" + (f" / 磁盘批次文件 {len(disk)}" if disk else ""))
            if rec:
                kbdir = kb_dir(gd)
                items_all: dict[str, dict] = {}
                for k in rec:
                    for it in parse_batch(out, int(k))["items"]:
                        items_all[it["id"]] = it
                print(f"  已入库批次：{sorted(int(k) for k in rec)}"
                      f"；知识库 {len(st.get('angles') or [])} 个角度文件 / "
                      f"{sum(len(read_bullets(angle_path(kbdir, a))) for a in (st.get('angles') or []))} 条")
                print(f"  {mark_summary(kbdir, items_all)}")
                if disk and (set(disk) - set(int(k) for k in rec)):
                    print(f"  → 可追加：{sorted(set(disk) - set(int(k) for k in rec))}")
            else:
                print("  知识库：还没有")
                if disk:
                    print(f"  → 可建库：{disk}")
            print()
        return 0
    out = ROOT / args.group / "提炼"
    st = A.load_json(out / STATE_NAME, {})
    for a in st.get("angles") or []:
        bl = read_bullets(angle_path(kb_dir(ROOT / args.group), a))
        print(f"  {a['no']:02d} {a['name']:<20} {len(bl):>3} 条")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把已提炼批次按角度合成多文件知识库（增量补丁）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--group", required=True)
        p.add_argument("--prompt", help=f"合成提示词（缺省沿用台账里的；首次缺省 {DEFAULT_PROMPT}）")
        p.add_argument("--model", help="llm_config.json 里的模型 key")
        p.add_argument("--max-tokens", type=int, default=8000)
        p.add_argument("--per-call", type=int, default=4, help="每次调用最多处理几批（默认 4）")
        p.add_argument("--dry-run", action="store_true", help="只生成输入不调模型")
        p.add_argument("--force", action="store_true", help="忽略指纹/已存在的冲突")

    p_init = sub.add_parser("init", help="首次建库（把已提炼批次作为初始补丁流）")
    common(p_init)
    p_init.add_argument("--batches", help="收录哪些批次，如 022,023（缺省=磁盘上全部）")
    p_init.set_defaults(func=cmd_build, mode="init", rebuild=False)

    p_add = sub.add_parser("add", help="把未入库批次追加进知识库")
    common(p_add)
    p_add.add_argument("--batches", help="只追加这些批次（缺省=所有未入库的）")
    p_add.add_argument("--rebuild", action="store_true",
                       help="清空角度文件、用已有批次结果重放（换合成提示词后用；不重新提炼）")
    p_add.set_defaults(func=cmd_build, mode="add", rebuild=False)

    p_ck = sub.add_parser("check", help="核对来源记号/疑似重复/代号残留，不调模型")
    p_ck.add_argument("--group", required=True)
    p_ck.set_defaults(func=cmd_check)

    p_rh = sub.add_parser("rehead", help="人工精修后重算各文件头部与索引（不调模型）")
    p_rh.add_argument("--group", required=True)
    p_rh.set_defaults(func=cmd_rehead)

    p_q = sub.add_parser("quotes", help="重抓原文留存（长文原文，不调模型）")
    p_q.add_argument("--group", required=True)
    p_q.set_defaults(func=cmd_quotes)

    p_st = sub.add_parser("status", help="所有群的知识库进度")
    p_st.add_argument("--group", help="只看某个群的角度分布")
    p_st.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    cj.force_utf8_console()
    raise SystemExit(main())
