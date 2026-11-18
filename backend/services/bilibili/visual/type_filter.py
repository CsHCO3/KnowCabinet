# -*- coding: utf-8 -*-
"""画面类型过滤：一次 VLM 调用看 8 张（700px + 附近字幕），判 8 类并给 keep。
   黑名单：人脸/现场照片、动画插画、标题卡、普通软件界面、复述字幕的页。
"""
from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from backend.config.settings import Settings  # noqa: E402
from .paths import PROJECT_ROOT


def _mkdtemp(prefix=None, suffix=None, dir=None):
    base = Path(dir or str(PROJECT_ROOT / "temp"))
    p = base / ((prefix or "tmp_") + uuid.uuid4().hex[:8])
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


tempfile.mkdtemp = _mkdtemp


def _sheet(sub, path):
    cols, tw, th = 2, 700, 394
    rows = (len(sub) + cols - 1) // cols
    cv = Image.new("RGB", (cols * (tw + 10) + 10, rows * (th + 28) + 10), (16, 16, 16))
    dr = ImageDraw.Draw(cv)
    for k, f in enumerate(sub):
        r, cc = divmod(k, cols)
        x, y = 10 + cc * (tw + 10), 10 + r * (th + 28)
        cv.paste(Image.open(f).convert("RGB").resize((tw, th)), (x, y))
        dr.text((x + 6, y + th + 6), f"#{k} {float(f.stem[1:-1]):.0f}s",
                fill=(245, 245, 245))
    cv.save(path, quality=88)


def _json_array(txt: str) -> list:
    """模型偶尔会连着吐两个数组（[..]\n[..]），贪婪正则会把两段粘起来炸掉。
    这里取第一个完整数组，后面的忽略。"""
    start = (txt or "").find("[")
    if start < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(txt[start:])
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


SLIDE_PROMPT = """这是同一个视频的 {n} 张候选画面（编号 #0 起，按图下编号）。每张的附近字幕：
{ctx}

逐张判断：这张画面是不是【一页幻灯片/PDF/讲义】的完整展示？

slide=1 必须同时满足：
1) 画面主体就是"一整页内容"（角落里可以有主播小窗，但主体必须是那页文档）；
2) 有标题 + 成段正文 / 条目列表 / 表格 / 示意图；
3) 版式像 PPT、PDF、讲义导出的一页，四周有留白、黑边或版式边框。

slide=0 的情况（宁可判 0，不要给"看起来像文档"的画面凑数）：
- 应用界面：聊天窗口、编辑器、IDE、终端、浏览器、App 页面——有窗口边框、工具栏、
  输入框、菜单，那是在"操作软件"，哪怕整屏白底、字很多；
- 动效信息卡：几块色块/卡片 + 一句大标题，没有成段正文；
- 标题卡、片头片尾、空镜、纯人像。

只输出 JSON 数组：[{{"i":0,"slide":0,"why":"10字内"}}]"""


def classify_slide(frames: list[Path], sub_at, batch: int = 8,
                   model_key: str = "glm-4.6v_ocr") -> dict:
    """二分类：这一帧是不是整页幻灯片/PDF/讲义。返回 {t: {slide, why}}。"""
    c = Settings().get_llm_config(model_key)
    out = {}
    if not frames:
        return out
    tmpdir = Path(_mkdtemp("sld_"))
    for gi in range(0, len(frames), batch):
        sub = frames[gi:gi + batch]
        p = tmpdir / f"s{gi}.jpg"
        _sheet(sub, p)
        ctx = "\n".join(
            f"#{k} {float(f.stem[1:-1]):.0f}s 附近字幕：{sub_at(float(f.stem[1:-1]))}"
            for k, f in enumerate(sub))
        prompt = SLIDE_PROMPT.format(n=len(sub), ctx=ctx)
        b64 = base64.b64encode(p.read_bytes()).decode()
        payload = {"model": c.model, "max_tokens": 6000, "temperature": 0,
                   "messages": [{"role": "user", "content": [
                       {"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64}},
                       {"type": "text", "text": prompt}]}]}
        try:
            r = requests.post(c.endpoint,
                              headers={"Authorization": "Bearer " + c.api_key,
                                       "Content-Type": "application/json"},
                              json=payload, timeout=300)
            m = r.json()["choices"][0]["message"]
            txt = (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()
            got = [x for x in _json_array(txt)
                   if isinstance(x.get("i"), int) and 0 <= x["i"] < len(sub)]
            if not got:                      # 解析失败整批重来一次
                r = requests.post(c.endpoint,
                                  headers={"Authorization": "Bearer " + c.api_key,
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=300)
                m = r.json()["choices"][0]["message"]
                txt = (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()
                got = [x for x in _json_array(txt)
                       if isinstance(x.get("i"), int) and 0 <= x["i"] < len(sub)]
            for x in got:
                t = float(sub[x["i"]].stem[1:-1])
                out[t] = {"slide": int(x.get("slide", 0)), "why": x.get("why", "")}
        except Exception as e:
            print("  幻灯片判定批次失败:", str(e)[:100])
    return out


def classify(frames: list[Path], sub_at, batch: int = 8,
             model_key: str = "glm-4.6v_ocr") -> dict:
    """frames: 按时间排序的 jpg 列表；sub_at(t)->str。
       返回 {t: {type, keep, why}}，失败返回 {}。"""
    c = Settings().get_llm_config(model_key)
    out = {}
    if not frames:
        return out
    tmpdir = Path(_mkdtemp("cls_"))
    for gi in range(0, len(frames), batch):
        sub = frames[gi:gi + batch]
        p = tmpdir / f"s{gi}.jpg"
        _sheet(sub, p)
        ctx = "\n".join(
            f"#{k} {float(f.stem[1:-1]):.0f}s 附近字幕：{sub_at(float(f.stem[1:-1]))}"
            for k, f in enumerate(sub))
        prompt = f"""这是同一个视频的 {len(sub)} 张候选画面（编号 #0 起，按图下编号）。每张的附近字幕：
{ctx}

逐张判断：
1) type 只能选一个：架构图/流程图、数据图表、文字要点页、提示词或文档截图、软件界面、人脸/现场照片、动画插画、标题卡
2) keep：
   1 = 画面里有【可读的成段文字】或【结构图/表】：提示词原文、文档正文、代码、参数表、要点清单、流程图、架构图
   0 = 人脸、现场照片、标题卡、动画、空镜、只有按钮/菜单/窗口框的软件界面
关键：只要画面里能看清成段的字或结构，就算字幕已经说过同样的内容，也要留 1-
   观众要的是原文和结构，视频里的转述不算数。
只输出 JSON 数组：[{{"i":0,"type":"...","keep":0,"why":"10字内"}}]"""
        b64 = base64.b64encode(p.read_bytes()).decode()
        payload = {"model": c.model, "max_tokens": 6000, "temperature": 0,
                   "messages": [{"role": "user", "content": [
                       {"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64}},
                       {"type": "text", "text": prompt}]}]}
        try:
            r = requests.post(c.endpoint,
                              headers={"Authorization": "Bearer " + c.api_key,
                                       "Content-Type": "application/json"},
                              json=payload, timeout=300)
            m = r.json()["choices"][0]["message"]
            txt = (m.get("content") or "").strip() or (m.get("reasoning_content") or "").strip()
            for x in _json_array(txt):   # 模型连吐两个数组时贪婪正则会炸，交给 _json_array
                if isinstance(x.get("i"), int) and 0 <= x["i"] < len(sub):
                    t = float(sub[x["i"]].stem[1:-1])
                    out[t] = {"type": x.get("type", ""), "keep": int(x.get("keep", 1)),
                              "why": x.get("why", "")}
        except Exception as e:
            print("  类型过滤批次失败:", str(e)[:100])
    return out
