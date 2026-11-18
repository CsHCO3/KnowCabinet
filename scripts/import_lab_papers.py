# -*- coding: utf-8 -*-
"""导入「实验室内」批次的 16 篇论文到 infosystem KB（幂等，copy 不 move 源）。

来源: C:/Users/HP/Desktop/开题新/搜集相关论文/实验室内
目标:
  - knowledgebase/full/papers/{id}/paper.md   (+ metadata.json)
  - data/papers/{id}/paper.pdf  images/

用法:
  python scripts/import_lab_papers.py            # 全部
  python scripts/import_lab_papers.py --ids a,b  # 指定 id
  python scripts/import_lab_papers.py --dry      # 只打印计划不落盘
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.archive.service import (  # noqa: E402
    _BIN_PAPERS, _FULL_PAPERS, _ensure_metadata_paper, _merge_frontmatter,
)

SRC = Path(r"C:\Users\HP\Desktop\开题新\搜集相关论文\实验室内")
RP = ROOT / "data" / "remote_pdf" / "15bbf0b29043"  # 远程 Marker 转换产物
BATCH_TAG = "实验室内"  # 本批次统一标签（/rag 页 tags 过滤选定这一批用）

# ── 16 篇论文清单 ────────────────────────────────────────────
# meta 字段: title/authors(列表)/year/venue/doi/source/source_type/section/tags/abstract
# files 字段: md / pdf / images（相对 SRC 或绝对；可选，缺失自动跳过）
PAPERS: list[dict] = [
    # ── ① 有 DOI，走 resolve_by_doi 增强（authors/abstract 由 resolver 补，下方为兜底） ──
    dict(id="10.1109_sose.2016.66", doi="10.1109/sose.2016.66",
         meta=dict(title="A System View of Financial Blockchains", year=2016,
                   venue="2016 IEEE Symposium on Service-Oriented System Engineering (SOSE)",
                   source="https://doi.org/10.1109/sose.2016.66",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "金融区块链", "系统架构", "共识"]),
         files=dict(pdf="03-A-System-View-of-Financial-Blockchains.pdf",
                    md=str(RP / "03-A-System-View-of-Financial-Blockchains.md"),
                    images_tar=str(RP / "03-A-System-View-of-Financial-Blockchains.tar"))),
    dict(id="10.1109_compsac.2018.00025", doi="10.1109/compsac.2018.00025",
         meta=dict(title="SPESC: A Specification Language for Smart Contracts", year=2018,
                   venue="2018 IEEE 42nd Annual Computer Software and Applications Conference (COMPSAC)",
                   source="https://doi.org/10.1109/compsac.2018.00025",
                   source_type="paper", section="Software Engineering",
                   tags=["区块链", "智能合约", "SPESC", "合约语言"]),
         files=dict(pdf="04_SPESC规范语言_秦博涵.pdf",
                    md=str(RP / "04_SPESC规范语言_秦博涵.md"),
                    images_tar=str(RP / "04_SPESC规范语言_秦博涵.tar"))),
    dict(id="10.1109_tr.2021.3054617", doi="10.1109/tr.2021.3054617",
         meta=dict(title="TA-SPESC: Toward Asset-Driven Smart Contract Language Supporting Ownership Transaction and Rule-Based Generation on Blockchain", year=2021,
                   venue="IEEE Transactions on Reliability", source="https://doi.org/10.1109/tr.2021.3054617",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "智能合约", "SPESC", "资产驱动", "权属交易"]),
         files=dict(pdf="06_TA-SPESC_王迪.pdf",
                    md=str(RP / "06_TA-SPESC_王迪.md"),
                    images_tar=str(RP / "06_TA-SPESC_王迪.tar"))),
    dict(id="10.1109_tr.2019.2948713", doi="10.1109/tr.2019.2948713",
         meta=dict(title="Cryptographic Attribute-Based Access Control (ABAC) for Secure Decision Making of Dynamic Policy With Multiauthority Attribute Tokens", year=2019,
                   venue="IEEE Transactions on Reliability", source="https://doi.org/10.1109/tr.2019.2948713",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "访问控制", "ABAC", "属性基", "多权威"]),
         files=dict(pdf="07_ABAC动态策略_于汝云.pdf",
                    md=str(RP / "07_ABAC动态策略_于汝云.md"),
                    images_tar=str(RP / "07_ABAC动态策略_于汝云.tar"))),
    dict(id="10.1109_access.2020.2979746", doi="10.1109/access.2020.2979746",
         meta=dict(title="Token-Based Access Control", year=2020,
                   venue="IEEE Access", source="https://doi.org/10.1109/access.2020.2979746",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "访问控制", "Token", "令牌"]),
         files=dict(pdf="08_Token-Based_Access_Control_甘国华.pdf",
                    md=str(RP / "08_Token-Based_Access_Control_甘国华.md"),
                    images_tar=str(RP / "08_Token-Based_Access_Control_甘国华.tar"))),
    dict(id="10.1109_access.2020.3036004", doi="10.1109/access.2020.3036004",
         meta=dict(title="Security Assessment of Blockchain in Chinese Classified Protection of Cybersecurity", year=2020,
                   venue="IEEE Access", source="https://doi.org/10.1109/access.2020.3036004",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "安全评估", "等级保护", "等保"]),
         files=dict(pdf="09_IEEEAccess_区块链安全评估_王迪.pdf",
                    md=str(RP / "09_IEEEAccess_区块链安全评估_王迪.md"),
                    images_tar=str(RP / "09_IEEEAccess_区块链安全评估_王迪.tar"))),
    dict(id="10.1109_mc.2021.3091379", doi="10.1109/mc.2021.3091379",
         meta=dict(title="Blockchain-Based Software Architecture Development for Service Requirements With Smart Contracts", year=2021,
                   venue="Computer", source="https://doi.org/10.1109/mc.2021.3091379",
                   source_type="paper", section="Software Engineering",
                   tags=["区块链", "智能合约", "软件架构", "服务需求"]),
         files=dict(md="Blockchain-BasedSoftwareArchitectureDevelopmentforServiceRequirementsWithSmartContracts--.md",
                    pdf="Blockchain-BasedSoftwareArchitectureDevelopmentforServiceRequirementsWithSmartContracts--.pdf",
                    images="Blockchain-BasedSoftwareArchitectureDevelopmentforServiceRequirementsWithSmartContracts--")),
    dict(id="10.13374_j.issn2095-9389.2019.03.26.004", doi="10.13374/j.issn2095-9389.2019.03.26.004",
         meta=dict(title="区块链技术及其研究进展", year=2019,
                   venue="工程科学学报, 41(11): 1361-1373", source="https://doi.org/10.13374/j.issn2095-9389.2019.03.26.004",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "共识算法", "智能合约", "综述"],
                   authors=["朱岩", "王巧石", "秦博涵", "王中豪"]),
         files=dict(pdf="02_区块链技术综述与进展_王巧石.pdf",
                    md=str(RP / "02_区块链技术综述与进展_王巧石.md"),
                    images_tar=str(RP / "02_区块链技术综述与进展_王巧石.tar"))),

    # ── ② 中文期刊（无 Crossref DOI，用自定义 slug） ──
    dict(id="cn_spesc_translator_2020",
         meta=dict(title="一种高级智能合约转化方法及竞买合约设计与实现", year=2020,
                   venue="计算机学报 (Chinese Journal of Computers)", source="CNKI 2020 (计算机学报)",
                   source_type="paper", section="Software Engineering",
                   tags=["区块链", "智能合约", "SPESC", "合约转化", "竞买合约"],
                   authors=["朱岩", "秦博涵", "陈娥", "刘国伟"]),
         files=dict(pdf="05_SPESC-Translator_秦博涵.pdf",
                    md=str(RP / "05_SPESC-Translator_秦博涵.md"),
                    images_tar=str(RP / "05_SPESC-Translator_秦博涵.tar"))),
    dict(id="cn_spesc_legal_2020",
         meta=dict(title="SPESC: 面向法律的智能合约设计与实践", year=2020,
                   venue="网络空间安全, 11(9)", source="CNKI 2020 (网络空间安全)",
                   source_type="paper", section="Software Engineering",
                   tags=["区块链", "智能合约", "SPESC", "法律合约"],
                   authors=["王迪", "秦博涵", "宋伟静", "朱岩"]),
         files=dict(pdf="10_SPESC面向法律的智能合约_王迪.pdf",
                    md=str(RP / "10_SPESC面向法律的智能合约_王迪.md"),
                    images_tar=str(RP / "10_SPESC面向法律的智能合约_王迪.tar"))),
    dict(id="cn_blockchain_security_arch_2017",
         meta=dict(title="区块链关键技术中的安全性研究", year=2017,
                   venue="", source="朱岩课题组论文（收稿 2016-10-03）",
                   source_type="paper", section="Security & Cryptography",
                   tags=["区块链", "安全", "可信", "隐私", "架构"],
                   authors=["朱岩", "甘国华", "邓迪", "姬菲菲", "陈爱平"]),
         files=dict(md="Security Architecture and Key Technologies of Blockchain.md",
                    images="Security Architecture and Key Technologies of Blockchain")),

    # ── ③ 硕士论文（自定义 thesis slug） ──
    dict(id="thesis_zhouzhiyuan_2019",
         meta=dict(title="面向区块链的交易基访问控制技术研究与实现", year=2019,
                   venue="北京科技大学硕士学位论文", source="USTB 硕士论文 S20170724",
                   source_type="thesis", section="Security & Cryptography",
                   tags=["区块链", "访问控制", "交易基", "硕论"],
                   authors=["周志远"]),
         files=dict(md="ustb硕论文S20170724周志远.md", images="ustb硕论文S20170724周志远")),
    dict(id="thesis_wangdi_2022",
         meta=dict(title="支持智能法律合约的资产模型与权属机制设计", year=2022,
                   venue="北京科技大学硕士学位论文", source="USTB 硕士论文 S20190698",
                   source_type="thesis", section="Security & Cryptography",
                   tags=["区块链", "智能合约", "资产模型", "权属", "法律合约", "硕论"],
                   authors=["王迪"]),
         files=dict(md="ustb硕论文王迪_正式版v6.md", images="ustb硕论文王迪_正式版v6")),
    dict(id="thesis_mengshutong_2024",
         meta=dict(title="基于智能法律合约的访问控制技术研究", year=2024,
                   venue="北京科技大学硕士学位论文", source="USTB 硕士论文 M202120827",
                   source_type="thesis", section="Security & Cryptography",
                   tags=["区块链", "智能合约", "访问控制", "法律合约", "硕论"],
                   authors=["孟疏桐"]),
         files=dict(md="毕业论文_孟疏桐.md", pdf="毕业论文_孟疏桐.pdf", images="毕业论文_孟疏桐")),
    dict(id="thesis_fanyuqing_2024",
         meta=dict(title="实数安全多方计算与比较协议研究", year=2024,
                   venue="北京科技大学硕士学位论文", source="USTB 硕士论文 M202110624",
                   source_type="thesis", section="Security & Cryptography",
                   tags=["多方计算", "安全比较协议", "实数", "硕论"],
                   authors=["范雨晴"]),
         files=dict(md="毕业论文_范雨晴.md", images="毕业论文_范雨晴")),

    # ── ④ 专利（自定义 patent slug） ──
    dict(id="patent_201911059491",
         meta=dict(title="一种基于脚本的属性基访问策略表示与执行方法及系统", year=2019,
                   venue="中国发明专利", source="国家知识产权局 申请号 201911059491.6",
                   source_type="patent", section="Security & Cryptography",
                   tags=["专利", "属性基", "访问控制", "ABAC", "脚本语言"],
                   authors=["北京科技大学"]),
         files=dict(md="2-全-一种基于脚本的属性基访问策略表示与执行方法及系统.md",
                    pdf="2-全-一种基于脚本的属性基访问策略表示与执行方法及系统.pdf",
                    images="2-全-一种基于脚本的属性基访问策略表示与执行方法及系统")),
]


def _resolve_doi_meta(doi: str) -> dict | None:
    """用 OpenAlex 解析 DOI 元数据（作者/摘要/引用），失败返回 None。"""
    try:
        from backend.services.arxiv.meta_resolver import resolve_by_doi
        return resolve_by_doi(doi)
    except Exception:
        return None


def import_paper(entry: dict, dry: bool = False) -> dict:
    pid = entry["id"]
    meta = dict(entry["meta"])
    files = entry.get("files") or {}
    log: list[str] = []

    # DOI 增强（非空字段才覆盖；保留手工兜底字段）
    if entry.get("doi"):
        rm = _resolve_doi_meta(entry["doi"])
        if rm:
            for k in ("title", "year", "venue", "citations", "abstract", "authors"):
                if rm.get(k):
                    meta.setdefault(k, rm[k])
    meta["id"] = pid
    meta.setdefault("source_type", "paper")
    if entry.get("doi"):
        meta.setdefault("doi", entry["doi"])
    # 批次统一标签（整批标记，/rag 页 tags 过滤选定这一批）
    if BATCH_TAG not in (meta.get("tags") or []):
        meta["tags"] = (meta.get("tags") or []) + [BATCH_TAG]

    full_dir = _FULL_PAPERS / pid
    bin_dir = _BIN_PAPERS / pid
    if dry:
        return {"id": pid, "title": meta.get("title", ""), "plan": True, "log": []}

    full_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    # 1) md → full/papers/{id}/paper.md（已有不同 → 备份 _kb_legacy）
    md_src = files.get("md")
    if md_src:
        sp = Path(md_src) if Path(md_src).is_absolute() else SRC / md_src
        if sp.exists():
            pmd = full_dir / "paper.md"
            if pmd.exists():
                # 比较时剥离 frontmatter（系统在 paper.md 上补过 frontmatter，与源文件天然不同）
                from backend.services.archive.service import _FRONTMATTER_RE
                def _body(path: Path) -> str:
                    t = path.read_text(encoding="utf-8", errors="replace")
                    m = _FRONTMATTER_RE.match(t)
                    return t[m.end():] if m else t
                if _body(pmd) != _body(sp):
                    from backend.services.archive.service import _KB_LEGACY
                    bk = _KB_LEGACY / pid
                    bk.mkdir(parents=True, exist_ok=True)
                    (bk / sp.name).write_text(sp.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                    log.append(f"[冲突] paper.md 正文不同 → 备份 _kb_legacy")
                else:
                    log.append("[跳过] paper.md 相同")
            else:
                shutil.copy2(str(sp), str(pmd))
                log.append(f"[md] → paper.md")
            _merge_frontmatter(pmd, meta, pid)
        else:
            log.append(f"[缺] md 不存在: {sp.name}")

    # 2) pdf → data/papers/{id}/paper.pdf
    pdf_src = files.get("pdf")
    if pdf_src:
        sp = Path(pdf_src) if Path(pdf_src).is_absolute() else SRC / pdf_src
        if sp.exists():
            dpdf = bin_dir / "paper.pdf"
            if not dpdf.exists():
                shutil.copy2(str(sp), str(dpdf))
                log.append("[pdf] → paper.pdf")
            else:
                log.append("[pdf] 已存在，跳过")
        else:
            log.append(f"[缺] pdf 不存在: {sp.name}")

    # 3) images → data/papers/{id}/images/（目录 或 转换产物 tar，平铺提取）
    img_dir = bin_dir / "images"
    img_src = files.get("images")
    img_tar = files.get("images_tar")
    if img_src or img_tar:
        img_dir.mkdir(parents=True, exist_ok=True)
    if img_src:
        sp = Path(img_src) if Path(img_src).is_absolute() else SRC / img_src
        if sp.is_dir():
            n = 0
            for img in sp.glob("*"):
                if img.is_file() and not (img_dir / img.name).exists():
                    shutil.copy2(str(img), str(img_dir / img.name))
                    n += 1
            if n:
                log.append(f"[img] 复制 {n} 图")
        elif sp.exists():
            log.append(f"[缺] images 是文件非目录: {sp.name}")
    if img_tar:
        sp = Path(img_tar) if Path(img_tar).is_absolute() else SRC / img_tar
        if sp.exists():
            import tarfile
            n = 0
            with tarfile.open(str(sp)) as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        target = img_dir / Path(m.name).name
                        if not target.exists():
                            with tf.extractfile(m) as fsrc:
                                target.write_bytes(fsrc.read())
                            n += 1
            if n:
                log.append(f"[img] tar 解包 {n} 图")
        else:
            log.append(f"[缺] images_tar 不存在: {sp.name}")

    # 4) metadata.json
    _ensure_metadata_paper(full_dir, pid, meta)
    log.append("[meta] metadata.json 刷新")

    return {"id": pid, "title": meta.get("title", ""), "log": log}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=None, help="逗号分隔的 id 子集")
    ap.add_argument("--dry", action="store_true", help="只打印计划")
    args = ap.parse_args()

    papers = PAPERS
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        papers = [p for p in PAPERS if p["id"] in want]

    ok = 0
    for p in papers:
        r = import_paper(p, dry=args.dry)
        if args.dry:
            print(f"  [计划] {r['id']}: {r['title']}")
            continue
        status = "✅" if r["log"] else "⚠️"
        print(f"{status} {r['id']}")
        for line in r["log"]:
            print(f"    {line}")
        ok += 1
    print(f"\n处理 {ok}/{len(papers)} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
