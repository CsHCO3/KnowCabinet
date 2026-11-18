"""T3 真实数据集成（marker=real，全部只读）：对 02 §2.3 基线核数 + I1/I10 指纹。

跑法：`pytest backend/tests -q -m real`
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.services.kb.catalog import derive_stage, derive_tier
from backend.services.kb.db import PROJECT_ROOT, connect
from backend.services.kb.ftx import match
from backend.services.kb.invariants import assert_invariants
from backend.services.kb.inventory import snapshot
from backend.tests.conftest import assert_snapshot, file_fingerprint, tree_fingerprint

pytestmark = pytest.mark.real

KB_ROOT = PROJECT_ROOT / "knowledgebase"
# 2026-10-20 结构重构：分区优先（default/ + CBAC/ + dairy/ + community/），
# `full/` 与顶层 `dense/` 取消；QQ 知识库从 data/ 镜像进 community/QQ。
# 真相源 = 这些目录下的 md/json，一律在 knowledgebase/ 里。
TRUTH_ROOTS = [
    KB_ROOT / "default" / "papers",
    KB_ROOT / "default" / "papers_meta",
    KB_ROOT / "default" / "dense",
    KB_ROOT / "default" / "research_projects",
    KB_ROOT / "CBAC" / "papers",
    KB_ROOT / "CBAC" / "papers_meta",
    KB_ROOT / "CBAC" / "dense",
    KB_ROOT / "dairy",
    KB_ROOT / "community" / "QQ",
    KB_ROOT / "community" / "tieba" / "knowledge",
    KB_ROOT / "community" / "bilibili",
]
# 2026-10-20：`full/`（docs、1-3-VERA、3-IMA、papers、CBAC）与顶层 `dense/` 全部下线，
# 由 `test_real_directory_layers_retired` 守"不得复活"。data/ 不再放任何参与检索的内容。
#
# 2026-10-21：旧的 FAISS 向量层（`ragsearch/index/vector/**`）连同 `/rag` 页、`rag-ask`、
# `ragsearch-index` 一起下线删除——向量现在只在 `data/kb/kb.db` 的 `kb_vec` 里。
# 原 `test_real_old_index_untouched`（I10 前半句"旧 FAISS 索引指纹不变"）随目录消失而删除；
# I10 后半句（`ragsearch/registry.json` 指纹）仍然有效，见下方 `test_real_old_registry_untouched`。
OLD_REGISTRY = PROJECT_ROOT / "ragsearch" / "registry.json"


@pytest.fixture(scope="module")
def conn():
    connection = connect()
    yield connection
    connection.close()


# ── 基线核数（02 §2.3）────────────────────────────────────────────────────


def test_real_scan_baseline_papers(conn):
    """02 §2.3 口径核实（2026-10-20 结构重构后重写）。

    分区优先后论文分两处：
      `default/papers/`  61 个目录（全有正文，60 个带 metadata.json）
      `CBAC/papers/`     74 个目录（72 个有正文）
    没有正文的题录不再占文件夹，扁平存 `papers_meta/<id>.json`：
      `default/papers_meta/` 1434 条、`CBAC/papers_meta/` 77 条。
    """
    d_dir = KB_ROOT / "default" / "papers"
    c_dir = KB_ROOT / "CBAC" / "papers"
    d_dirs = [p for p in d_dir.iterdir() if p.is_dir()]
    c_dirs = [p for p in c_dir.iterdir() if p.is_dir()]

    assert len(d_dirs) == 61, f"default/papers 目录数 {len(d_dirs)}"
    assert len([p for p in d_dirs if any(p.glob("*.md"))]) == 61, "default/papers 应全部有正文"
    assert len([p for p in d_dirs if (p / "metadata.json").exists()]) == 60
    assert len(list((KB_ROOT / "default" / "papers_meta").glob("*.json"))) == 1434

    assert len(c_dirs) == 74, f"CBAC/papers 目录数 {len(c_dirs)}"
    assert len([p for p in c_dirs if any(p.glob("*.md"))]) == 72
    assert len(list((KB_ROOT / "CBAC" / "papers_meta").glob("*.json"))) == 77

    shell = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE topic='papers' AND best_tier=0").fetchone()[0]
    assert shell == 1434, f"papers L0 题录入库数 {shell}"


def test_real_scan_baseline_topic(conn):
    # D44 密度序：CBAC 151 = L0 题录 77 + L1 原文 59 + L2 轻整理 15
    row = conn.execute(
        "SELECT COUNT(*), SUM(best_tier=0), SUM(best_tier=1), SUM(best_tier=2) "
        "FROM documents WHERE topic='cbac'").fetchone()
    assert tuple(row) == (151, 77, 59, 15), f"CBAC 口径 {tuple(row)}（期望 151=77+59+15）"


def test_real_vera_ima_removed(conn):
    for topic in ("vera", "ima"):
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE topic=?", (topic,)).fetchone()[0]
        assert count == 0, f"{topic} 应已从目录层删除（D31）"


def test_real_directory_layers_retired():
    """2026-10-20 结构重构：`full/` 与顶层 `dense/` 不得复活；下线库不得回到 registry。"""
    for name in ("full", "dense"):
        assert not (KB_ROOT / name).exists(), f"knowledgebase/{name} 已下线，不应再出现"
    for name in ("docs", "1-3-VERA", "3-IMA"):
        assert not (KB_ROOT / "full" / name).exists(), f"full/{name} 已下线，不应再出现"
    from ragsearch.config import load_registry

    registry = load_registry()
    for name in ("docs", "dense", "1-3-VERA", "1-3-VERA-projects", "3-IMA", "3-IMA-projects"):
        assert name not in registry, f"registry.json 仍登记已下线库 {name}"


def test_real_row_counts_baseline(conn):
    # 2026-10-18 基线刷新（数据新增，代码零改动）：+13 视频摘要（knowledgebase/dense/video，
    # M8 DoD-② 补验）+2 ai-agent 文档（对方线 landing）。见 knowledgebase/_meta/reports/acceptance/M8.log §4b。
    # 2026-10-19 基线刷新（数据删除，代码零改动）：-1 幻觉摘要
    # （BV1Ghpy6gE5x，48 字错误字幕被 LLM 编成《三国演义》张飞段落，经用户批准移除）。
    # 2026-10-20 基线刷新（一天内的清理迁移，全部经库主批准）：
    #   +3 条归位 CBAC（1 篇真论文 + 2 篇自研笔记）；-12 条移出 papers
    #   （8 条与 CBAC 重复 + 3 条归位 + 1 条并入既有 DOI 空壳）；
    #   -4 条 docs 记录（3 篇学位论文与 CBAC 重复、1 篇与 vera 副本重复）；
    #   -8 条 vera/ima 领域正文（库主"这个领域全删掉"）；
    #   -46 条开源项目调研（project.md 全是「待补全」模板，只留 HEIR 两份）；
    #   +1 条对方线贴吧知识点（`tieba-thread-11117873910`，非本轮改动）。
    # 2026-10-20 第二轮（结构重构 + data 清理）：
    #   分区优先重排（doc_id 全部不变，纯路径变更）；-20 条跨分区重复题录；
    #   -7 条撞名残留（`cbac:<id>` 之类的前缀行）；+18 篇从 data/pdfs 抢救出正文
    #   （原本只有题录的 L0 线索升为 L1 原文）；-1 条 `paper` 脏 id。
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("documents", "versions", "kb_fts")
    }
    assert counts == {"documents": 1784, "versions": 1800, "kb_fts": 1784}, counts


def test_real_topic_tier_distribution(conn):
    # M5：口径改为可检索状态（active + legacy_l2），否则 D27 的 5 条 dense 会被漏统计
    rows = conn.execute(
        "SELECT topic, best_tier, COUNT(*) FROM documents "
        "WHERE status IN ('active','legacy_l2') "
        "GROUP BY topic, best_tier ORDER BY topic, best_tier").fetchall()
    distribution = {f"{row[0]}|L{row[1]}": row[2] for row in rows}
    # D44 密度序（2026-10-20 清理后实测）：
    #   论文全文（paper）为 L1 原文，浓缩稿（condensation）为 L2 轻整理；
    #   群聊角度稿 + 贴吧知识点 + 视频三遍法为 L3。
    assert distribution.get("papers|L0") == 1434
    assert distribution.get("papers|L1") == 58
    assert distribution.get("papers|L2") == 3
    assert distribution.get("cbac|L0") == 77
    assert distribution.get("cbac|L1") == 59
    assert distribution.get("cbac|L2") == 15
    assert distribution.get("research-projects|L1") == 2
    assert distribution.get("ai-agent|L1") == 1
    assert distribution.get("ai-agent|L3") == 13
    assert distribution.get("videos|L2") == 13
    assert distribution.get("videos|L3") == 4
    assert distribution.get("dairy|L1") == 23
    assert distribution.get("campus|L1") == 1
    assert distribution.get("campus|L3") == 4
    assert distribution.get("_inbox|L1") == 77


# ── D32 组织标记 ───────────────────────────────────────────────────────────


def test_real_d32_org_mark_landed(conn):
    """用户裁定 v2语言论证 = CBAC 二级分组：必须在 org_marks、不在 tags。"""
    in_tags = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE EXISTS "
        "(SELECT 1 FROM json_each(documents.tags) WHERE json_each.value='v2语言论证')"
    ).fetchone()[0]
    in_org = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE EXISTS "
        "(SELECT 1 FROM json_each(documents.org_marks) WHERE json_each.value='v2语言论证')"
    ).fetchone()[0]
    assert in_tags == 0, f"tags 中仍有 {in_tags} 条 v2语言论证"
    assert in_org == 25, f"org_marks 中 v2语言论证 {in_org} 条（期望 25）"


def test_real_inventory_groups_by_org_marks(conn):
    """D32：体检必须能按 org_marks 给出 topic 内二级分组。"""
    orgs = {item["org"]: item["count"] for item in snapshot("cbac")["org_counts"]}
    assert orgs.get("v2语言论证") == 25, orgs
    assert orgs.get("实验室内") == 58, orgs


def test_real_ask_org_filter_hits_project_line(conn):
    """D32：kb-ask --org 过滤后必须仍能取到该线的论文。"""
    from backend.services.kb.query import ask
    result = ask("XACML policy evaluation", org=["v2语言论证"], mode="deep", top_k=10)
    assert result["groups"], "项目线过滤后无命中"
    assert all("v2语言论证" in group["org_marks"] for group in result["groups"])
    assert result["groups"][0]["topic"] == "cbac"


# ── FTS 冒烟 ───────────────────────────────────────────────────────────────


def test_real_fts_smoke_english_l0(conn):
    """04 §7 原写「判决树 命中 ≥5 条 L0」——实测该词在库中根本不存在（M3 已修订）。

    改用英文词验证同一命题：英文查询能命中 L0 空壳（旧系统必然 0 命中）。
    """
    hits = match(conn, "decision tree", {"tier_min": 0, "tier_max": 0},
                 limit=500, meta_only=True)
    assert len(hits) >= 5, f"L0 空壳命中仅 {len(hits)} 条"


def test_real_fts_smoke_spesc(conn):
    hits = match(conn, "SPESC", {}, limit=500)
    assert len(hits) >= 3, f"SPESC 命中 {len(hits)} 条"


def test_real_inventory_latency(conn):
    """DoD-1：单 topic 体检 <3s。"""
    started = time.perf_counter()
    data = snapshot("cbac")
    elapsed = time.perf_counter() - started
    assert data["total_docs"] == 151
    assert elapsed < 3.0, f"体检耗时 {elapsed:.2f}s 超预算 3s"


# ── 真相源与旧索引：没被动过（P0）─────────────────────────────────────────


def test_real_truth_unchanged():
    """I1：真相源快照必须完全一致（P0，任何里程碑不得豁免）。

    M3 基线快照在 D32 组织标记落盘**之后**采集；此后 truth 文件一律不得再变。
    """
    current = {root.name or str(root): tree_fingerprint(root) for root in TRUTH_ROOTS}
    assert_snapshot("truth_snapshot_m3", current)


def test_real_old_registry_untouched():
    """I10 后半句：旧 RAG 的 `ragsearch/registry.json` 指纹不变。

    M8 复核发现原本只守了 `index/vector/**`，而该文件相对 git HEAD 已有 +6 行
    （M2 期 `dairy_digital_twin` 登记，属合法变更，已随该快照一次性认领）。
    该文件决定旧 `rag-ask` 读哪些库、BM25 权重与 tag_boost——被误改会**静默降质**且无告警，
    故必须进 I10。指纹不记 mtime 并归一化换行，避免 git 触文件造成假红灯。

    2026-10-20 第二次认领（经用户批准）：删掉 `docs` 与 `1-3-VERA` / `1-3-VERA-projects` /
    `3-IMA` / `3-IMA-projects` 五条登记（对应目录层已下线）。快照随
    `KB_UPDATE_SNAPSHOT=1` 显式刷新；除这五条外文件其余内容未动。
    """
    assert_snapshot("old_registry_fingerprint_m8", file_fingerprint(OLD_REGISTRY))


# ── 派生层自洽 ─────────────────────────────────────────────────────────────


def test_real_invariants_hold(conn):
    report = assert_invariants(conn)
    # 不变式本身是"active == fts"；基线随数据增删与口径重扫同步
    assert report["active_documents"] == report["fts_rows"] == 1784
    assert report["legacy_l2_documents"] == 5, "D27 存量豁免名单应为 5 条 dense"
    assert report["orphan_versions"] == 0
    assert report["best_tier_mismatches"] == 0
    assert report["invalid_tags"] == 0


def test_real_tier_stage_derivation_round_trip(conn):
    """kind→tier 必须在真库上自洽，且以 knowledgebase/_meta/config/tiers.json 为唯一事实源。"""
    from backend.services.kb.catalog import load_tiers

    kinds = load_tiers()
    rows = conn.execute("SELECT kind, tier FROM versions").fetchall()
    unknown = sorted({row["kind"] for row in rows if row["kind"] not in kinds})
    assert not unknown, f"库中存在未登记 kind：{unknown}"
    bad = [(row["kind"], row["tier"]) for row in rows
           if kinds.get(row["kind"]) != int(row["tier"])]
    assert not bad, f"tier 与分级表不符：{bad[:5]}"


# ── M4：arxiv_cats 分流 + chunk/向量对账 ─────────────────────────────────────


def test_real_arxiv_cats_split(conn):
    """M4：org_marks 中 cs.* 分区码必须已分流到 arxiv_cats，md 源文件未动。"""
    in_org = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE EXISTS "
        "(SELECT 1 FROM json_each(documents.org_marks) WHERE json_each.value LIKE 'cs.%')"
    ).fetchone()[0]
    in_cats = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE EXISTS "
        "(SELECT 1 FROM json_each(documents.arxiv_cats) WHERE json_each.value LIKE 'cs.%')"
    ).fetchone()[0]
    assert in_org == 0, f"org_marks 仍有 {in_org} 条 cs.* 分区码"
    assert in_cats == 13, f"arxiv_cats 中 cs.* 文档数 {in_cats}（期望 13）"


def test_real_kb_vec_i9(conn):
    """M4：I9 容器向量数 == 已嵌 chunks 数；CBAC 至少已有 chunk manifest。"""
    report = assert_invariants(conn)
    assert report["embedded_chunks"] == report["container_vectors"]
    assert report["missing_vectors"] == 0
    assert report["orphan_vectors"] == 0
    cbac_chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks c JOIN versions v ON v.version_id=c.version_id "
        "JOIN documents d ON d.doc_id=v.doc_id WHERE d.topic='cbac'").fetchone()[0]
    assert cbac_chunks > 0, "CBAC 尚未生成 chunk manifest"


def test_real_eval_set_labels_all_exist(conn):
    """评测集每条期望 doc_id 都必须真实存在（防手工改评测集引入幽灵目标）。"""
    from backend.services.kb.evalx import load_set, natural_id
    from backend.services.kb.ftx import SEARCHABLE_STATUSES
    placeholders = ",".join("?" * len(SEARCHABLE_STATUSES))
    known = {
        row[0] for row in conn.execute(
            f"SELECT doc_id FROM documents WHERE status IN ({placeholders})",
            SEARCHABLE_STATUSES)
    }
    natural = {natural_id(doc) for doc in known}
    missing = [
        (item["id"], doc)
        for item in load_set()
        for doc in item["expect_doc_ids"]
        if doc not in known and natural_id(doc) not in natural
    ]
    assert not missing, f"评测集含不存在的 doc_id: {missing}"


# ── M5：legacy_l2 豁免 + 段落取证 + eval_v2 ────────────────────────────────


def test_real_legacy_l2_dense_docs(conn):
    """D27：5 条 dense 无回链 L2 标 legacy_l2；仍可检索（在 kb_fts 里）且免回链。"""
    from backend.services.kb.catalog import legacy_l2_ids
    frozen = legacy_l2_ids()
    assert len(frozen) == 5, f"冻结名单应为 5 条，实测 {len(frozen)}"
    rows = {
        row["doc_id"]: row["status"]
        for row in conn.execute(
            f"SELECT doc_id, status FROM documents WHERE doc_id IN "
            f"({','.join('?' * len(frozen))})", sorted(frozen))
    }
    assert rows == {doc_id: "legacy_l2" for doc_id in frozen}, rows
    fts_rows = conn.execute(
        f"SELECT COUNT(*) FROM kb_fts WHERE doc_id IN ({','.join('?' * len(frozen))})",
        sorted(frozen),
    ).fetchone()[0]
    assert fts_rows == 5, "legacy_l2 文档必须仍在 FTS 索引内（可检索）"


def test_real_legacy_l2_evidence_exempt(conn):
    """D44：legacy_l2 只作豁免标记透出，不改变 quote_from。

    台账上的 5 篇 legacy_l2 是"存量无回链 L2"；密度序下它们没有 tier=1 原文，
    因此 quote_from=derived、verbatim_allowed=False。
    """
    from backend.services.kb.query import ask
    groups = {group["doc_id"]: group for group in ask("Blockchain", mode="browse",
                                                     top_k=10)["groups"]}
    legacy = [group for group in groups.values()
              if group["evidence"].get("legacy_l2")]
    assert legacy, "browse 应至少命中 1 篇 legacy_l2 文档"
    for group in legacy:
        assert "must_quote_l3" not in group["evidence"], "旧字段必须已移除"
        assert group["evidence"]["quote_from"] in {"raw", "derived"}
        if group["evidence"]["quote_from"] == "derived":
            assert group["evidence"]["verbatim_allowed"] is False


def test_real_fetch_chunk_segment_roundtrip(conn):
    """M5 段落级取证：kb-ask 给的 matched_chunks[].chunk_id 能直接喂 kb-fetch。

    D44：可引原话的是 tier=1（原文），不再是 tier=3。
    """
    from backend.services.kb.query import ask, fetch
    result = ask("smart legal contract specification language", mode="deep", top_k=10)
    chunks = [chunk for group in result["groups"] for chunk in group.get("matched_chunks") or []]
    assert chunks, "deep 模式应给出命中块"
    raw_chunks = [chunk for chunk in chunks if chunk["tier"] == 1]
    assert raw_chunks, "应至少命中一个原文(tier=1)块"
    target = raw_chunks[0]
    payload = fetch(target_chunk_doc(conn, target["chunk_id"]), tier=1,
                    chunk_ids=[target["chunk_id"]], context=1)
    assert "error" not in payload, payload
    segment = payload["segments"][0]
    assert segment["chunk_id"] == target["chunk_id"]
    assert segment["tier"] == 1 and segment["text"]
    assert segment["total_chunks"] >= segment["chunk_index"] + 1
    assert payload["content"] == "", "段落取证不给整篇"


def target_chunk_doc(conn, chunk_id: int) -> str:
    row = conn.execute("SELECT doc_id FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    assert row is not None
    return row["doc_id"]


def test_real_eval_v2_structure(conn):
    """M5 验收：eval_v2 = 50 条，含 ≥10 条跨语言（纯中文 query → 英文 L3 论文）。"""
    from backend.services.kb.evalx import load_set
    entries = load_set("knowledgebase/_meta/eval/eval_v2.jsonl")
    assert len(entries) == 50, f"eval_v2 应 50 条，实测 {len(entries)}"
    assert len({item["id"] for item in entries}) == 50
    v1_ids = {item["id"] for item in load_set("knowledgebase/_meta/eval/eval_v1.jsonl")}
    assert v1_ids <= {item["id"] for item in entries}, "eval_v1 30 条必须原样保留"
    cross = [item for item in entries if item.get("cross_lingual")]
    assert len(cross) >= 10, f"跨语言条目仅 {len(cross)} 条"
    import re
    latin = re.compile(r"[0-9A-Za-z]")
    for item in cross:
        assert item["mode"] == "deep", f"{item['id']} 跨语言必须 deep"
        assert not latin.search(item["query"]), f"{item['id']} 跨语言 query 不应含拉丁词"
    l0_cases = [item for item in entries if item.get("tier_expect") == "L0"]
    assert len(l0_cases) >= 5, f"L0 反例仅 {len(l0_cases)} 条"
    from backend.services.kb.ftx import SEARCHABLE_STATUSES
    placeholders = ",".join("?" * len(SEARCHABLE_STATUSES))
    known = {
        row[0] for row in conn.execute(
            f"SELECT doc_id FROM documents WHERE status IN ({placeholders})",
            SEARCHABLE_STATUSES)
    }
    for item in entries:
        for doc_id in item["expect_doc_ids"]:
            assert doc_id in known, f"{item['id']}: 幽灵目标 {doc_id}"
