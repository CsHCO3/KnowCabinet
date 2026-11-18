# -*- coding: utf-8 -*-
"""2-2.png → Visio (.vsdx) 复刻初稿生成器 v2

不依赖任何第三方 vsdx 库，手工构建最小合规 OPC 包（原生 Visio XML）：
矩形框占位 + 直线（带箭头）+ 文字，保证 Visio 能正常打开渲染。
同时输出 layout JSON 和 matplotlib 预览图（自查布局）。

用法：
    python scripts/gen_visio_2_2.py

输出：
    output/visio/2-2_layout.json
    output/visio/2-2_draft.vsdx
    output/visio/2-2_preview.png
"""
import json
import math
import os
import zipfile
from xml.sax.saxutils import escape

ROOT = r"D:\infosystem"
OUT_DIR = os.path.join(ROOT, "output", "visio")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_VSDX = os.path.join(OUT_DIR, "2-2_draft_v2.vsdx")
OUT_JSON = os.path.join(OUT_DIR, "2-2_layout.json")
OUT_PNG = os.path.join(OUT_DIR, "2-2_preview.png")

PAGE_W, PAGE_H = 11.0, 8.5  # 英寸，横版

# ────────────────────────── 布局定义 ──────────────────────────
# Visio 坐标：英寸，原点在页面左下角，y 向上
nodes = []  # kind: rect(带框) / label(纯文字，无框无填充)
edges = []  # 直线段；arrow=False 为无箭头线段


def rect(nid, cx, cy, w, h, text="", fill="#FFFFFF", line="#595959",
         rounding=0.06, dashed=False, label=False, text_color=None,
         size=7.0):
    nodes.append(dict(id=nid, kind="label" if label else "rect", cx=cx, cy=cy,
                      w=w, h=h, text=text, fill=fill, line=line,
                      rounding=rounding, dashed=dashed,
                      text_color=text_color, size=size))


def edge(x1, y1, x2, y2, arrow=True, color="#404040"):
    edges.append(dict(x1=x1, y1=y1, x2=x2, y2=y2, arrow=arrow, color=color))


PT = 1.0 / 72.0  # 点 → 英寸

# ---- 底部：密码学基石（紫色横条，横跨全宽）----
rect("crypto_bar", 5.5, 0.5, 10.0, 0.5,
     "密码学基石：ZKP（零知识证明）+ MK-FHE（多密钥同态加密）+ 多项式平滑逼近",
     fill="#9673A6", line="#764E8C", text_color="#FFFFFF", size=9.0)

# ---- 左侧：参与方（客户端）大框，内含 参与方1/2/N 竖排 ----
rect("p_container", 1.6, 4.5, 2.0, 6.5, "", fill="#F5F5F5", line="#7F7F7F")
rect("p_title", 1.6, 7.25, 1.8, 0.45, "参与方（客户端）", label=True, size=10.0)
P_Y = {"P1": 6.3, "P2": 4.9, "PN": 3.5}
for k, y in P_Y.items():
    rect("part_" + k, 1.6, y, 1.5, 0.6, "参与方" + k[-1])
rect("p_dots", 1.6, 4.15, 0.6, 0.3, "...", label=True)

# ---- 中间：3 个一样的"本地数据/模型"框（竖排），下面各挂"本地训练/Agent交互" ----
# 挂接关系（不是连线）：小框中心点 = 大框底边正中心
COL_X = 3.9
for k, y in P_Y.items():
    rect("col_" + k, COL_X, y, 1.5, 0.85, "本地数据/模型", size=8.0)
    rect("hang_" + k, COL_X, y - 0.425 - 0.2, 1.3, 0.4,
         "本地训练/Agent交互", fill="#D5E8D4", size=6.0)
    # 参与方 → 数据/模型框
    edge(2.6, y, 3.15, y)
    # 密态更新(ZKP证明)：短箭头 → 图标 → 短箭头，文字在图标上方（无框）
    edge(4.65, y, 5.19, y)
    rect("icon_" + k, 5.3, y, 0.22, 0.32, "", fill="#FFFFFF")
    rect("upd_" + k, 5.3, y + 0.30, 0.8, 0.22, "密态更新", label=True, size=6.5)
    edge(5.41, y, 5.9, y)

# ---- 右侧：密态联合建模服务器（聚合方）大框 ----
rect("server", 8.2, 4.5, 4.6, 6.5, "", fill="#FFE6CC", line="#D79B00")
rect("s_title", 8.2, 7.25, 3.6, 0.45, "密态联合建模服务器（聚合方）",
     label=True, size=10.0)

TOP_Y = 6.2
for nid, x, t in [("s1", 6.55, "① ZKP\n合法性验证"),
                  ("s2", 7.65, "② 行为一致性\n检测(密态)"),
                  ("s3", 8.75, "③ 风险评分\n与选择(密态)"),
                  ("s4", 9.85, "④ 连续化鲁棒\n聚合(密态)")]:
    rect(nid, x, TOP_Y, 0.95, 0.7, t, fill="#DAE8FC", size=6.5)
edge(7.025, TOP_Y, 7.175, TOP_Y)   # ①→②
edge(8.125, TOP_Y, 8.275, TOP_Y)   # ②→③
edge(9.225, TOP_Y, 9.375, TOP_Y)   # ③→④

BOT_Y = 4.3
rect("feat", 7.65, BOT_Y, 1.1, 0.95,
     "多维异常特征\n参数偏离度\n行为偏差\n损失变化", fill="#FFE6CC", size=6.5)
rect("risk", 8.7, BOT_Y, 1.1, 0.95,
     "风险评分与权重\n风险分 ri\n选择量 si\n自适应权重 wi", fill="#D5E8D4", size=6.5)
rect("agg", 9.9, BOT_Y, 1.05, 0.95,
     "聚合结果\n(密态全局\n模型更新)", fill="#E1D5E7", line="#9673A6",
     dashed=True, size=6.5)

edge(7.65, 5.85, 7.65, 4.775)    # ②→多维异常特征
edge(8.7, 5.85, 8.7, 4.775)      # ③→风险评分与权重
edge(9.85, 5.85, 9.85, 4.775)    # ④→聚合结果
rect("cycle_label", 10.33, 5.31, 0.75, 0.22, "全周期(密态)",
     label=True, size=6.0)
edge(9.25, BOT_Y, 9.375, BOT_Y)  # 风险评分与权重→聚合结果

# ---- 全局反馈（密态）：聚合结果 → 底部折线 → 参与方（复刻修正：指向参与方）----
edge(9.9, 3.825, 9.9, 1.8, arrow=False)
edge(9.9, 1.8, 2.6, 1.8)
rect("fb_label", 5.7, 2.02, 1.6, 0.3, "全局反馈（密态）", label=True, size=8.0)

# ────────────────────────── 手工构建 vsdx ──────────────────────────


def _f(v):
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _pt(v):
    return f"{v * PT:.5f}"


def _rect_shape(sid, n):
    cx, cy, w, h = n["cx"], n["cy"], n["w"], n["h"]
    if n["kind"] == "label":
        line_cells = '<Cell N="LinePattern" V="0"/>'
        fill_cells = '<Cell N="FillPattern" V="0"/>'
        rounding = ""
    else:
        line_cells = (f'<Cell N="LineWeight" V="{_pt(1.1)}"/>'
                      f'<Cell N="LineColor" V="{n["line"]}"/>')
        fill_cells = f'<Cell N="FillForegnd" V="{n["fill"]}"/>'
        rounding = '<Cell N="Rounding" V="0.05"/>' if n.get("rounding") else ""
        if n.get("dashed"):
            line_cells += '<Cell N="LinePattern" V="2"/>'
    char_cells = f'<Cell N="Size" V="{_pt(n["size"])}"/>'
    if n.get("text_color"):
        char_cells += f'<Cell N="Color" V="{n["text_color"]}"/>'
    text = f"<Text>{escape(n['text'])}</Text>" if n["text"] else ""
    return (f'<Shape ID="{sid}" Type="Shape">'
            f'<XForm><PinX>{_f(cx)}</PinX><PinY>{_f(cy)}</PinY>'
            f'<Width>{_f(w)}</Width><Height>{_f(h)}</Height>'
            f'<LocPinX>{_f(w / 2)}</LocPinX><LocPinY>{_f(h / 2)}</LocPinY>'
            f'<Angle>0</Angle><FlipX>0</FlipX><FlipY>0</FlipY>'
            f'<ResizeMode>0</ResizeMode></XForm>'
            f'<Line>{line_cells}{rounding}</Line>'
            f'<Fill>{fill_cells}</Fill>'
            f'<Section N="Character"><Row IX="0">{char_cells}</Row></Section>'
            f'<Section N="Geometry" IX="0">'
            f'<Cell N="NoFill" V="0"/><Cell N="NoLine" V="0"/>'
            f'<Cell N="NoShow" V="0"/><Cell N="NoSnap" V="0"/>'
            f'<Row T="RelMoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
            f'<Row T="RelLineTo" IX="2"><Cell N="X" V="1"/><Cell N="Y" V="0"/></Row>'
            f'<Row T="RelLineTo" IX="3"><Cell N="X" V="1"/><Cell N="Y" V="1"/></Row>'
            f'<Row T="RelLineTo" IX="4"><Cell N="X" V="0"/><Cell N="Y" V="1"/></Row>'
            f'<Row T="RelLineTo" IX="5"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
            f'</Section>{text}</Shape>')


def _edge_shape(sid, e):
    x1, y1, x2, y2 = e["x1"], e["y1"], e["x2"], e["y2"]
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    angle = math.atan2(dy, dx)
    arrow = ('<Cell N="EndArrow" V="4"/><Cell N="EndArrowSize" V="1"/>'
             if e["arrow"] else "")
    return (f'<Shape ID="{sid}" Type="Shape">'
            f'<XForm><PinX>{_f((x1 + x2) / 2)}</PinX>'
            f'<PinY>{_f((y1 + y2) / 2)}</PinY>'
            f'<Width>{_f(length)}</Width><Height>0</Height>'
            f'<LocPinX>{_f(length / 2)}</LocPinX><LocPinY>0</LocPinY>'
            f'<Angle>{_f(angle)}</Angle><FlipX>0</FlipX><FlipY>0</FlipY>'
            f'<ResizeMode>0</ResizeMode></XForm>'
            f'<XForm1D><BeginX>{_f(x1)}</BeginX><BeginY>{_f(y1)}</BeginY>'
            f'<EndX>{_f(x2)}</EndX><EndY>{_f(y2)}</EndY></XForm1D>'
            f'<Line><Cell N="LineWeight" V="{_pt(1.1)}"/>'
            f'<Cell N="LineColor" V="{e["color"]}"/>{arrow}</Line>'
            f'<Section N="Geometry" IX="0">'
            f'<Cell N="NoFill" V="1"/><Cell N="NoLine" V="0"/>'
            f'<Cell N="NoShow" V="0"/><Cell N="NoSnap" V="0"/>'
            f'<Row T="MoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
            f'<Row T="LineTo" IX="2"><Cell N="X" V="{_f(length)}"/>'
            f'<Cell N="Y" V="0"/></Row></Section></Shape>')


def build_vsdx():
    shapes = []
    sid = 1
    for n in nodes:
        shapes.append(_rect_shape(sid, n))
        sid += 1
    for e in edges:
        shapes.append(_edge_shape(sid, e))
        sid += 1

    ns = "http://schemas.microsoft.com/office/visio/2012/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"

    page1 = (f"<?xml version='1.0' encoding='utf-8'?>"
             f"<PageContents xmlns='{ns}' xmlns:r='{r}' xml:space='preserve'>"
             f"<Shapes>{''.join(shapes)}</Shapes></PageContents>")

    pages = (f"<?xml version='1.0' encoding='utf-8'?>"
             f"<Pages xmlns='{ns}' xmlns:r='{r}' xml:space='preserve'>"
             f"<Page ID='0' NameU='页-1' Name='页-1'>"
             f"<PageSheet>"
             f"<Cell N='PageWidth' V='{_f(PAGE_W)}'/>"
             f"<Cell N='PageHeight' V='{_f(PAGE_H)}'/>"
             f"<Cell N='PageScale' V='1'/><Cell N='DrawingScale' V='1'/>"
             f"<Cell N='DrawingSizeType' V='3'/>"
             f"<Cell N='DrawingScaleType' V='0'/>"
             f"</PageSheet><Rel r:id='rId1'/></Page></Pages>")

    document = (f"<?xml version='1.0' encoding='utf-8'?>"
                f"<VisioDocument xmlns='{ns}' xml:space='preserve'>"
                f"<DocumentSettings TopPage='0'/></VisioDocument>")

    pages_rels = (f"<?xml version='1.0' encoding='utf-8'?>"
                  f"<Relationships xmlns='{pkg}'>"
                  f"<Relationship Id='rId1' Type='http://schemas.microsoft.com"
                  f"/visio/2010/relationships/page' Target='page1.xml'/>"
                  f"</Relationships>")

    doc_rels = (f"<?xml version='1.0' encoding='utf-8'?>"
                f"<Relationships xmlns='{pkg}'>"
                f"<Relationship Id='rId1' Type='http://schemas.microsoft.com"
                f"/visio/2010/relationships/pages' Target='pages/pages.xml'/>"
                f"<Relationship Id='rId2' Type='http://schemas.microsoft.com"
                f"/visio/2010/relationships/windows' Target='windows.xml'/>"
                f"</Relationships>")

    root_rels = (f"<?xml version='1.0' encoding='utf-8'?>"
                 f"<Relationships xmlns='{pkg}'>"
                 f"<Relationship Id='rId1' Type='http://schemas.microsoft.com"
                 f"/visio/2010/relationships/document' "
                 f"Target='visio/document.xml'/>"
                 f"<Relationship Id='rId2' Type='http://schemas.openxmlformats"
                 f".org/package/2006/relationships/metadata/core-properties' "
                 f"Target='docProps/core.xml'/>"
                 f"<Relationship Id='rId3' Type='http://schemas.openxmlformats"
                 f".org/officeDocument/2006/relationships/extended-properties' "
                 f"Target='docProps/app.xml'/></Relationships>")

    content_types = (f"<?xml version='1.0' encoding='utf-8'?>"
                     f"<Types xmlns='{ct}'>"
                     f"<Default Extension='rels' ContentType='application/vnd"
                     f".openxmlformats-package.relationships+xml'/>"
                     f"<Default Extension='xml' ContentType='application/xml'/>"
                     f"<Override PartName='/visio/document.xml' ContentType="
                     f"'application/vnd.ms-visio.drawing.main+xml'/>"
                     f"<Override PartName='/visio/pages/pages.xml' ContentType="
                     f"'application/vnd.ms-visio.pages+xml'/>"
                     f"<Override PartName='/visio/pages/page1.xml' ContentType="
                     f"'application/vnd.ms-visio.page+xml'/>"
                     f"<Override PartName='/visio/windows.xml' ContentType="
                     f"'application/vnd.ms-visio.windows+xml'/>"
                     f"<Override PartName='/docProps/core.xml' ContentType="
                     f"'application/vnd.openxmlformats-package.core-properties+xml'/>"
                     f"<Override PartName='/docProps/app.xml' ContentType="
                     f"'application/vnd.openxmlformats-officedocument"
                     f".extended-properties+xml'/></Types>")

    core = ("<?xml version='1.0' encoding='utf-8'?>"
            "<cp:coreProperties xmlns:cp='http://schemas.openxmlformats.org"
            "/package/2006/metadata/core-properties' "
            "xmlns:dc='http://purl.org/dc/elements/1.1/'>"
            "<dc:title>2-2 复刻初稿</dc:title></cp:coreProperties>")
    app = ("<?xml version='1.0' encoding='utf-8'?>"
           "<Properties xmlns='http://schemas.openxmlformats.org"
           "/officeDocument/2006/extended-properties'>"
           "<Application>infosystem gen_visio_2_2</Application></Properties>")

    windows = (f"<?xml version='1.0' encoding='utf-8'?>"
               f"<Windows xmlns='{ns}' ClientWidth='1600' ClientHeight='900' "
               f"xml:space='preserve'>"
               f"<Window ID='0' WindowType='Drawing' WindowState='1073741824' "
               f"WindowLeft='0' WindowTop='0' WindowWidth='1600' "
               f"WindowHeight='900' ContainerType='0' Page='0' "
               f"ViewScale='1' ViewCenterX='{_f(PAGE_W / 2)}' "
               f"ViewCenterY='{_f(PAGE_H / 2)}'>"
               f"<ShowRulers>1</ShowRulers><ShowGrid>1</ShowGrid>"
               f"<ShowPageBreaks>0</ShowPageBreaks><ShowGuides>0</ShowGuides>"
               f"<ShowConnectionPoints>0</ShowConnectionPoints>"
               f"<GlueSettings>9</GlueSettings>"
               f"<SnapSettings>65847</SnapSettings>"
               f"<SnapExtensions>34</SnapExtensions><SnapAngles/>"
               f"<DynamicGridEnabled>1</DynamicGridEnabled>"
               f"<TabExtensions/><TabExtensionTypes/>"
               f"</Window></Windows>")

    with zipfile.ZipFile(OUT_VSDX, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("visio/document.xml", document)
        z.writestr("visio/_rels/document.xml.rels", doc_rels)
        z.writestr("visio/windows.xml", windows)
        z.writestr("visio/pages/pages.xml", pages)
        z.writestr("visio/pages/_rels/pages.xml.rels", pages_rels)
        z.writestr("visio/pages/page1.xml", page1)
    print(f"[ok] vsdx → {OUT_VSDX}")


def save_json():
    data = dict(page=dict(width=PAGE_W, height=PAGE_H),
                nodes=nodes, edges=edges)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[ok] json → {OUT_JSON}")


def build_preview():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False
        from matplotlib.patches import Rectangle, FancyArrow
    except ImportError:
        print("[warn] matplotlib 不可用，跳过预览")
        return
    fig, ax = plt.subplots(figsize=(PAGE_W, PAGE_H))
    ax.set_xlim(0, PAGE_W)
    ax.set_ylim(0, PAGE_H)
    ax.axis("off")
    for n in nodes:
        if n["kind"] == "label":
            ax.text(n["cx"], n["cy"], n["text"], ha="center", va="center",
                    fontsize=n["size"])
        else:
            ax.add_patch(Rectangle((n["cx"] - n["w"] / 2,
                                    n["cy"] - n["h"] / 2),
                                   n["w"], n["h"], facecolor=n["fill"],
                                   edgecolor=n["line"], linewidth=1.2,
                                   linestyle="--" if n["dashed"] else "-"))
            ax.text(n["cx"], n["cy"], n["text"], ha="center", va="center",
                    fontsize=n["size"])
    for e in edges:
        dx, dy = e["x2"] - e["x1"], e["y2"] - e["y1"]
        ax.add_patch(FancyArrow(e["x1"], e["y1"], dx, dy,
                                width=0.008,
                                head_width=0.12 if e["arrow"] else 0.001,
                                head_length=0.12 if e["arrow"] else 0.001,
                                length_includes_head=True, color=e["color"]))
    fig.savefig(OUT_PNG, dpi=150, bbox_inches="tight")
    print(f"[ok] preview → {OUT_PNG}")


if __name__ == "__main__":
    save_json()
    build_preview()
    build_vsdx()
