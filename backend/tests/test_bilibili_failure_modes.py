"""字幕失败模式（2026-11-06 审计）：瞬时 vs 业务性、重试口径、无字幕时的跳过。

对应三个被审计出来的缺口：
1. 瞬时错误表漏了坏轨/空轨两种（import 线遇到坏轨不重试）；
2. ensure_subtitle 无差别重试，业务性失败（确实没字幕）白等 ~12s；
3. digest.outline 对空字幕 max() 崩溃 → 现在返回 None（跳过，不入库）。

纯 mock，不联网、不写真实缓存目录。
"""
from __future__ import annotations

import pytest

from backend.services.bilibili import subtitle
from backend.services.bilibili.visual import digest, runner


# ── 1. 瞬时性判定 ───────────────────────────────────────────

@pytest.mark.parametrize("err", [
    "view API failed",
    "Subtitle file is empty or unsupported",
    "Request failed",
    "Download subtitle failed: timeout",
    "No subtitle url found",
    "subtitle timeline exceeds video duration (306s > 211s): track likely belongs to another video",
])
def test_transient_errors_are_retryable(err):
    assert subtitle.is_transient_error(err) is True


@pytest.mark.parametrize("err", [
    "No Chinese subtitle track (only foreign/AI-translation)",
    "Invalid Bilibili URL or BV id",
    "BILI_COOKIE not configured",
    "page out of range: 3 (total 2)",
    "",
])
def test_business_errors_are_not_retryable(err):
    assert subtitle.is_transient_error(err) is False


# ── 2. ensure_subtitle 的重试口径 ────────────────────────────

@pytest.fixture
def cache_in_tmp(tmp_path, monkeypatch):
    """把缓存路径指到 tmp，别污染 data/bilibili/subtitles。"""
    monkeypatch.setattr(runner, "subtitle_cache_path",
                        lambda bv, page=1: tmp_path / f"{bv}.json")
    return tmp_path


def test_business_failure_fails_fast(cache_in_tmp, monkeypatch):
    calls = []
    monkeypatch.setattr(
        subtitle, "get_bilibili_timestamped_subtitle",
        lambda url, page=None: (calls.append(url), {"ok": False, "error": "No Chinese subtitle track"})[1])
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(RuntimeError) as ei:
        runner.ensure_subtitle("BVtest")
    assert "业务性失败" in str(ei.value)
    assert len(calls) == 1        # 不重试
    assert slept == []            # 也不睡


def test_transient_failure_retries_then_succeeds(cache_in_tmp, monkeypatch):
    seq = [{"ok": False, "error": "No subtitle url found"},
           {"ok": True, "segments": [{"start": 0, "end": 1, "text": "hi"}],
            "duration": 1, "page": 1, "page_count": 1, "page_title": "", "title": "t"}]

    def fake(url, page=None):
        return seq.pop(0)

    monkeypatch.setattr(subtitle, "get_bilibili_timestamped_subtitle", fake)
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    r = runner.ensure_subtitle("BVtest")
    assert r["ok"] is True
    assert slept == [3]          # 只重试一次就成功
    assert (cache_in_tmp / "BVtest.json").exists()


def test_all_transient_failures_raise_after_three(cache_in_tmp, monkeypatch):
    calls = []
    monkeypatch.setattr(
        subtitle, "get_bilibili_timestamped_subtitle",
        lambda url, page=None: (calls.append(1), {"ok": False, "error": "view API failed"})[1])
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as ei:
        runner.ensure_subtitle("BVtest")
    assert "已重试 3 次" in str(ei.value)
    assert len(calls) == 3


# ── 3. 没有字幕时不崩、不写脏条目 ────────────────────────────

def test_outline_without_segments_returns_empty():
    assert digest.outline([]) == []


def test_digest_skips_when_no_subtitle(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    monkeypatch.setattr(digest.P, "load_segments", lambda bv, page=1, **kw: [])
    assert digest.digest_one("BVnope") is None
    assert not (tmp_path / "BVnope").exists()      # 没建目录、没留半成品


# ── 4. 包内相对导入（回归：曾是裸导入，干净进程必炸）────────────

def test_digest_internal_import_is_package_relative():
    """digest._same_section 里曾写 `from dedup_pages import text_sim`。

    那行能解析，只是因为 seg_filter 导入时把 visual/ 塞进了 sys.path；任何
    "没跑扫帧段就进浓缩"的路径（--from digest、visual: none）在干净进程里
    都会 ModuleNotFoundError。这里直接调，等于锁住那个契约。
    """
    from backend.services.bilibili.visual.digest import _same_section

    # 注意：text_sim 对不足 6 个 bigram 的短文本直接返回 0，所以这里必须用长串
    title = "Agent 架构设计的三层拆解"
    body = "第一层讲调度，第二层讲工具调用，第三层讲记忆怎么存"
    assert _same_section({"title": title, "body": body}, title, body) is True
    assert _same_section({"title": "完全不搭界的另一种说法在这里",
                          "body": "这一段讲的是别的事情，和上面没有任何关系"},
                         "数据库索引的底层结构", "B+ 树的页分裂与合并过程") is False


# ── 5. 扫帧总开关（--scan off）：一条都不许扫 ────────────────

def _stub_pipeline(monkeypatch, tmp_path, calls, *,
                   budget=(False, "全片没有一处判成整页文档"),
                   probe_hits=0, page_count=1):
    """把 run_one 的 IO 与 LLM 全换成桩。

    桩一律写成 *a/**k：上游函数签名加参数（例如 page）时，测试不该跟着一起崩。
    """
    monkeypatch.setattr(runner, "ensure_subtitle",
                        lambda *a, **k: {"title": "t", "page_count": page_count,
                                         "page_title": "P1", "page": 1})
    monkeypatch.setattr(runner, "push_desc_resources", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "subtitle_usable", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(runner, "get_sections", lambda *a, **k: {"sections": [1]})
    monkeypatch.setattr(runner, "cleanup", lambda *a, **k: None)
    monkeypatch.setattr(runner, "apply_kb", lambda *a, **k: True)
    monkeypatch.setattr(runner, "subtitle_cache_path",
                        lambda *a, **k: tmp_path / f"{a[0]}.json")
    for name in ("BVscan", "BVveto", "BVmulti", "BVsingle"):
        (tmp_path / f"{name}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "visual_budget",
                        lambda *a, **k: (calls.append("budget"), budget)[1])
    monkeypatch.setattr(runner, "subtitle_coverage",
                        lambda *a, **k: (calls.append("coverage"), 1.0)[1])
    monkeypatch.setattr(runner, "probe_page_count",
                        lambda *a, **k: (probe_hits, f"抽样 {probe_hits} 帧命中"))
    monkeypatch.setattr(runner.seg_filter, "scan",
                        lambda *a, **k: (calls.append("scan"),
                                         {"keep_frames": [{"t": 1.0, "file": "a.jpg"}],
                                          "n_candidates": 3})[1])
    monkeypatch.setattr(runner, "ROOT", tmp_path)    # 别去翻真实产物目录里的 report_thr*

    from backend.services.bilibili.visual import digest
    monkeypatch.setattr(
        digest, "digest_one",
        lambda *a, **k: (calls.append(f"digest:{a[1] if len(a) > 1 else k.get('thr')}"),
                         {"path": tmp_path / "d.md", "chars": 10, "images": 0})[1])


def test_scan_off_skips_all_visual_work(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls, budget=(True, "12/14 段判成整页文档"))

    res = runner.run_one("BVscan", 3, "v4flash", "fetch", scan_mode="off")
    assert res["status"] == "ok"
    assert res["visual_gate"] == "scan off"
    assert "budget" not in calls and "scan" not in calls and "coverage" not in calls
    assert "digest:none" in calls        # 没有帧 → 直接走纯字幕浓缩


# ── 6. 没装 opencv 时去重不能整个失效 ────────────────────────

def test_same_page_tolerates_missing_cv2_keys():
    """没装 cv2 时 _orb_feats 返回 (None, None)：same_page 必须安全返回"不同页"，
    而不是抛异常把整个去重步骤带走。"""
    from backend.services.bilibili.visual.dedup_pages import same_page

    assert same_page((None, None), (None, None)) == (False, "", 0.0)


# ── 7. 8 帧抽样否决：分段说有页，但一张都不是 → 不扫 ─────────

def test_probe_veto_skips_scan_when_zero_frames(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls,
                   budget=(True, "12/14 段判成整页文档"), probe_hits=0)
    res = runner.run_one("BVveto", 3, "v4flash", "fetch")
    assert res["probe_hits"] == 0
    assert "scan" not in calls
    assert res["n_keep"] == 0
    assert "否决" in res["visual_gate"]
    assert "digest:none" in calls


def test_probe_veto_keeps_scan_when_one_frame_hits(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls,
                   budget=(True, "12/14 段判成整页文档"), probe_hits=1)
    res = runner.run_one("BVveto", 3, "v4flash", "fetch")
    assert "scan" in calls            # 1 张也放行，宁可多扫不可漏扫
    assert res["n_keep"] == 1
    assert "digest:3" in calls


def test_probe_veto_can_be_disabled(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls,
                   budget=(True, "12/14 段判成整页文档"), probe_hits=0)
    res = runner.run_one("BVveto", 3, "v4flash", "fetch", probe_veto=False)
    assert "scan" in calls
    assert res["n_keep"] == 1


# ── 8. 多P：只处理 P1 时必须标记"不完整" ────────────────────

def test_multipart_video_is_marked_partial(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls, page_count=100)
    res = runner.run_one("BVmulti", 3, "v4flash", "fetch")
    assert res["partial"] is True
    assert res["page_count"] == 100


def test_single_page_video_is_not_partial(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls, page_count=1)
    res = runner.run_one("BVsingle", 3, "v4flash", "fetch")
    assert not res.get("partial")
    assert res.get("page_count", 1) == 1


# ── 9. --from digest 不能把带图条目降级成纯字幕 ──────────────

def test_from_digest_reuses_existing_report(tmp_path, monkeypatch):
    """跳过扫帧段时，磁盘上已有的 report_thr* 必须被沿用。

    否则 --from digest 会拿 thr=none 重写 digest，把带图版本覆盖成纯字幕版，
    再 apply 就连知识库里的图一起丢了。
    """
    import json as _json

    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls, budget=(True, "12/14 段判成整页文档"))
    d = tmp_path / "BVdigest"
    d.mkdir()
    (d / "report_thr3.json").write_text(
        _json.dumps({"keep_frames": [{"t": 1.0, "file": "a.jpg"}]}), encoding="utf-8")
    (tmp_path / "BVdigest.json").write_text("{}", encoding="utf-8")

    runner.run_one("BVdigest", 3, "v4flash", "digest")
    assert "digest:3" in calls
    assert "digest:none" not in calls


def test_from_digest_falls_back_to_none_without_report(tmp_path, monkeypatch):
    calls: list[str] = []
    _stub_pipeline(monkeypatch, tmp_path, calls, budget=(True, "12/14 段判成整页文档"))
    (tmp_path / "BVfresh.json").write_text("{}", encoding="utf-8")

    runner.run_one("BVfresh", 3, "v4flash", "digest")
    assert "digest:none" in calls
