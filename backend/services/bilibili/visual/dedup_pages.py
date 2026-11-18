# -*- coding: utf-8 -*-
"""信息点去重：优先图形学（小面积变化 / 网格相似 / 竖直滚动），其次 OCR 文本重合。
   把"同一张图的不同状态"合成一张，取信息最全的代表帧。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_gray(path, w=240) -> np.ndarray:
    im = Image.open(path).convert("L")
    h = max(1, int(im.height * w / im.width))
    return np.asarray(im.resize((w, h), Image.BILINEAR), dtype=np.float32)


def cell_profile(a: np.ndarray, gx: int = 16, gy: int = 10) -> np.ndarray:
    H, W = a.shape
    ch, cw = H // gy, W // gx
    return a[:gy * ch, :gx * cw].reshape(gy, ch, gx, cw).mean(axis=(1, 3))


def graphic_same(a: np.ndarray, b: np.ndarray):
    """返回 (是否同图, 依据, 分数)。a/b 尺寸必须一致。"""
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0]); w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    frac = float((np.abs(a - b) > 28).mean())
    if frac < 0.04:                       # 只动了红圈/光标这类小面积
        return True, "overlay", frac
    ma, mb = cell_profile(a), cell_profile(b)
    sim = float((np.abs(ma - mb) < 14).mean())
    if sim > 0.90 and frac < 0.12:        # 版面几乎一致，只有个别格子变了
        return True, "grid", sim
    H = a.shape[0]
    best = None
    for dy in range(8, H - 8, 4):         # 竖直滚动：找最佳平移
        o1, o2 = a[dy:], b[:H - dy]
        if o1.shape[0] < H * 0.4:
            break
        d = float(np.abs(o1 - o2).mean())
        if best is None or d < best[0]:
            best = (d, dy, o1.shape[0] / H)
    if best and best[0] < 9.0 and best[2] > 0.45:
        return True, "scroll", best[0]
    return False, "", frac


def bigrams(s: str) -> set:
    s = "".join(ch for ch in (s or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def text_sim(ta: str, tb: str) -> float:
    A, B = bigrams(ta), bigrams(tb)
    if len(A) < 6 or len(B) < 6:
        return 0.0
    return len(A & B) / min(len(A), len(B))


# ---------------- 跨算法：同一页面的判定 ----------------

TEXT_SIM_THR = 0.50    # 仅用于报告/诊断：OCR 二元组包含率
LOOSE_GAP = 30.0       # 特征松判只在 30 秒内生效，避免把整段视频串起来
SIFT_GOOD_MIN = 100    # SIFT ratio-test 有效匹配数（实测 同页 153~986，异页 <=48）
SIFT_RATIO = 0.75
SIFT_NFEAT = 1500
TEXT_MIN_LEN = 20      # 两边都读到这么多字，才允许用文字否决
TEXT_VETO = 0.35       # 二元组包含率低于此值 → 判为两张不同的页
TEXT_SAME = 0.75       # 高于此值 → 无视像素差异，判为同一页（红圈/高亮/光标）

_sift = None
_bf = None


_CV2_WARNED = False


def _init_sift():
    """初始化 SIFT。没装 opencv 时返回 (None, None)，让上层退回文字/图形去重。

    以前这里直接 import cv2，异常会一路抛到 seg_filter 的 try/except，
    结果是**整个去重步骤被跳过**（连不需要 cv2 的文字去重也不做了）。
    实测 2026-11-06：conda infosystem 环境没装 opencv，日志只有一行
    "去重跳过: No module named 'cv2'"，去重等于关闭。
    """
    global _sift, _bf, _CV2_WARNED
    if _sift is None:
        try:
            import cv2
        except ImportError:
            if not _CV2_WARNED:
                print("  ⚠ 未装 opencv（cv2）：SIFT 特征去重不可用，"
                      "已降级为「文字 + 图形」去重")
                _CV2_WARNED = True
            return None, None
        _sift = cv2.SIFT_create(nfeatures=SIFT_NFEAT)
        _bf = cv2.BFMatcher(cv2.NORM_L2)
    return _sift, _bf


def _orb_feats(path):
    """名字保留（外面在调）：实际用 SIFT，对 8~12px 的小字比 ORB 稳得多。
    统一缩到 640 宽再提特征，免得 360p/1080p 两种帧给出的匹配数没有可比性。"""
    sift, _ = _init_sift()
    if sift is None:                 # 没装 opencv：交给文字/图形去重
        return (None, None)
    import cv2
    im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return (None, None)
    im = im[: int(im.shape[0] * 0.90)]          # 去底部字幕带
    h, w = im.shape[:2]
    if w > 640:
        im = cv2.resize(im, (640, max(1, int(h * 640 / w))), interpolation=cv2.INTER_AREA)
    return sift.detectAndCompute(im, None)


def same_page(ka, kb, ta: str = "", tb: str = ""):
    """(kps, descs) 两帧是不是"同一页面的不同时刻"。返回 (是否同页, 依据, 分数)。
    同一页面的不同瞬间（滚动/叠人像/动画加元素/红圈批注）会留下大量一致的局部特征。
    但套同一套模板的幻灯片也有大量一致特征（头图/页脚/logo），所以文字要能否决：
    两边都读得出字、而字几乎不重合 → 两张不同的页。"""
    _, bf = _init_sift()
    if bf is None:
        return False, "", 0.0
    (kpa, da), (kpb, db) = ka, kb
    if da is not None and db is not None and len(kpa) > 20 and len(kpb) > 20:
        pairs = bf.knnMatch(da, db, k=2)
        good = [m for m, n in pairs if m.distance < SIFT_RATIO * n.distance]
        if len(good) >= SIFT_GOOD_MIN:
            if len(ta) >= TEXT_MIN_LEN and len(tb) >= TEXT_MIN_LEN \
                    and text_sim(ta, tb) < TEXT_VETO:
                return False, "text_veto", 0.0
            return True, "sift", len(good)
    return False, "", 0.0


def dedup(items: list[dict], text_thr: float = TEXT_SIM_THR,
          max_gap: float = 240.0, complete: bool = True):
    """items: [{'t','path','text'}]（必须按 t 升序）→ clusters(list[list[int]])
    判定顺序：图形学快速判（完全同图/小面积叠加/滚动）→ SIFT 特征一致性
    （同一页面被滚动、叠人像、动画加元素）。
    complete=True 用全链接聚类：一张新帧要跟簇里每一张都同页才并，
    避免 A≈B、B≈C 的链条把整段视频并成一簇。"""
    n = len(items)
    grays = [load_gray(it["path"]) for it in items]
    keys = [_orb_feats(it["path"]) for it in items]

    def linked(i, j):
        gap = items[j]["t"] - items[i]["t"]
        if gap > max_gap:
            return False, "", 0
        # 文字优先：同一页被画圈、加高亮、被鼠标划过，字是一样的 —— 那就是同一页。
        ta, tb = items[i].get("text", ""), items[j].get("text", "")
        if len(ta) >= 40 and len(tb) >= 40:
            ts = text_sim(ta, tb)
            if ts >= TEXT_SAME:
                return True, "text", ts
        same, why, sc = graphic_same(grays[i], grays[j])
        if same:
            return same, why, sc
        if gap <= LOOSE_GAP:
            return same_page(keys[i], keys[j], ta, tb)
        return False, "", 0

    memo: dict[tuple[int, int], tuple] = {}

    def sim(i, j):
        k = (i, j)
        if k not in memo:
            memo[k] = linked(i, j)
        return memo[k]

    if not complete:
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                if sim(i, j)[0]:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        return list(groups.values())

    clusters: list[list[int]] = []
    for j in range(n):
        placed = False
        for c in clusters:
            if items[j]["t"] - items[c[0]]["t"] > max_gap:
                continue
            if all(sim(i, j)[0] for i in c):
                items[j]["why"] = sim(c[-1], j)[1]
                c.append(j)
                placed = True
                break
        if not placed:
            clusters.append([j])

    # 第二轮：同一页分几段出现时（中间插了别的内容），分段聚类看不出是一页，
    # 但两段的代表帧是同一张。所以再拿代表帧比一次，同字就并簇。
    reps = [max(c, key=lambda i: items[i]["t"]) for c in clusters]
    m = len(reps)
    parent = list(range(m))

    def find2(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(m):
        for b in range(a + 1, m):
            ta = items[reps[a]].get("text", "")
            tb = items[reps[b]].get("text", "")
            if len(ta) >= 40 and len(tb) >= 40 and text_sim(ta, tb) >= TEXT_SAME:
                ra, rb = find2(a), find2(b)
                if ra != rb:
                    parent[rb] = ra
    merged: dict[int, list[int]] = {}
    for a in range(m):
        merged.setdefault(find2(a), []).extend(clusters[a])
    out = [sorted(c) for c in merged.values()]
    return sorted(out, key=lambda c: items[c[0]]["t"])


def pick_rep(cluster: list[int], items: list[dict]) -> int:
    """取"播完的那一版"：同一页面的最后一个瞬间（动画放完/批注叠完/滚动到位），
    也是唯一不会拿到半张图的选法。"""
    return max(cluster, key=lambda i: items[i]["t"])


# ---------------- 界面类重复收敛 ----------------

UI_TYPES = {"软件界面", "手机界面", "产品宣传/官网展示"}
SKEL_THR = 0.85          # 版面骨架（16x10 格均值）相似
TEXT_THR = 0.75          # OCR 二元组包含率


def ui_repeats(kept: list[dict], labels: dict | None = None,
               max_gap: float = 30.0, skel_thr: float = SKEL_THR,
               text_thr: float = TEXT_THR) -> tuple[list[dict], list[list[float]]]:
    """界面类帧的重复收敛：同类型 + 时间相邻(<=max_gap) + 骨架像或文字包含 → 只留信息最全的一张。
    只对 UI_TYPES 生效；架构图/文字要点页不在此列（同模板不同内容的幻灯片会被误并）。
    返回 (保留列表, 合并掉的簇)。
    labels: {t_str: {'type':...}}，有就用它覆盖 kept 里的 type。"""
    n = len(kept)
    if n < 2:
        return kept, []
    types = []
    for it in kept:
        lab = {}
        for k in (it["t"], f"{it['t']:.1f}", f"{it['t']:.2f}"):
            if labels and k in labels:
                lab = labels[k]
                break
        types.append(it.get("type") or lab.get("type", ""))

    grays = {}
    for i, it in enumerate(kept):
        if types[i] in UI_TYPES:
            grays[i] = load_gray(it["path"])

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if types[i] not in UI_TYPES:
            continue
        for j in range(i + 1, n):
            if types[j] != types[i] or kept[j]["t"] - kept[i]["t"] > max_gap:
                continue
            s = float((np.abs(cell_profile(grays[i]) - cell_profile(grays[j])) < 14).mean())
            same = s >= skel_thr
            if not same:
                same = text_sim(kept[i].get("text", ""), kept[j].get("text", "")) >= text_thr
            if same:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out, merged = [], []
    for g in groups.values():
        rep = max(g, key=lambda i: (len(kept[i].get("text", "")), kept[i]["t"]))
        out.append(kept[rep])
        if len(g) > 1:
            merged.append([round(kept[i]["t"], 1) for i in g])
    out.sort(key=lambda x: x["t"])
    return out, merged


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("--ocr", default="")
    a = ap.parse_args()
    d = Path(a.frames_dir)
    ocr = json.loads(Path(a.ocr).read_text(encoding="utf-8")) if a.ocr and Path(a.ocr).exists() else {}
    items = []
    for p in sorted(d.glob("t*.jpg"), key=lambda x: float(x.stem[1:-1])):
        t = float(p.stem[1:-1])
        items.append({"t": t, "path": str(p),
                      "text": (ocr.get(f"{t:.2f}s") or {}).get("text", "")})
    cl = dedup(items)
    print(f"{d.name}: {len(items)} 帧 → {len(cl)} 簇")
    for c in cl:
        print("  ", [round(items[i]["t"], 1) for i in c])


