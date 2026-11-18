"""处理原因分类：三种"没入库"必须能分开记，而且能判断值不值得补抓。"""
from backend.services.bilibili.reasons import (
    REASON_LABEL, classify_reason, is_retryable, subtitle_reason)


def test_subtitle_missing_vs_off_topic_vs_low_coverage():
    assert classify_reason("no_subtitle", "No subtitle url found") == "subtitle_missing"
    assert classify_reason("no_usable_subtitle",
                           "字幕不像这条视频的（字幕是减肥vlog）") == "subtitle_off_topic"
    assert classify_reason("no_usable_subtitle",
                           "字幕只覆盖 9%（24s/278s），判定不可用") == "subtitle_low_coverage"


def test_subtitle_reason_handles_unusable_text():
    assert subtitle_reason("字幕覆盖 100%，相关性 +3.2") == "subtitle_low_coverage"
    assert subtitle_reason("没有字幕缓存") == "subtitle_missing"
    assert subtitle_reason("别的原因") == "subtitle_unusable"


def test_visual_capture_failure_is_retryable_but_verdict_is_not():
    """画面抓错了能重抓；判成"没价值"是内容结论，重跑没用。"""
    assert classify_reason("scan_failed", "'t0'") == "visual_capture_failed"
    assert is_retryable("visual_capture_failed") is True
    assert is_retryable("visual_judged_useless") is False
    assert is_retryable("visual_scanned_none") is False


def test_digest_empty_vs_digest_failed():
    assert classify_reason("digest_failed", "审稿后什么都不剩") == "digest_empty"
    assert classify_reason("digest_failed", "其它") == "digest_failed"


def test_ok_and_unknown_codes():
    assert classify_reason("ok") == "ok"
    assert classify_reason("apply_failed", "apply_failed") == "apply_failed"
    assert classify_reason("weird_status", "") == "error"


def test_every_reason_has_a_label():
    for code in ("ok", "subtitle_missing", "subtitle_off_topic", "subtitle_low_coverage",
                 "visual_capture_failed", "visual_judged_useless", "visual_scanned_none",
                 "digest_empty", "digest_failed", "apply_failed", "error"):
        assert code in REASON_LABEL
