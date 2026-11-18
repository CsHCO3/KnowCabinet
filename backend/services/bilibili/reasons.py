"""处理结果的"原因"词表：把"字幕没抓到 / 字幕抓错 / 画面抓错 / 判定画面无价值"分开。

为什么单独一个模块：`runner` 会去连 B 站、载重模型，而查账（`bili-status`）只想知道
标签和可重试性，不该为此把 runner 整个拉起来。
"""

# 三件事必须能分开（用户 2026-11-09 要求）：
#   1) 字幕侧：没抓到 / 抓到了但内容不对 / 覆盖太低
#   2) 画面侧：抓取过程出错（可重试） / 扫到了但判定无价值（别重试）
#   3) 内容侧：纯字幕也能成条（正常结果，不是失败）
REASON_LABEL = {
    "ok": "入库成功",
    "asr_used": "靠本地 ASR 转写救回",
    "subtitle_missing": "字幕没抓到（无轨/接口无返回）",
    "subtitle_off_topic": "字幕抓错（内容是别的视频/歌词）",
    "subtitle_low_coverage": "字幕覆盖太低",
    "subtitle_too_short": "官方字幕太短（<200 字，壳子在内容空）",
    "no_speech": "全程无语音（ASR 判定，纯 BGM/UI 演示类）",
    "asr_unavailable": "ASR 环境不全（依赖/模型/显卡）",
    "asr_failed": "ASR 转写失败（下载/解码/OOM）",
    "asr_no_audio": "拿不到音频流（付费/充电专属或区域限制）",
    "subtitle_unusable": "字幕不可用（其它）",
    "visual_off": "按开关跳过画面",
    "visual_judged_useless": "判定画面无价值（未扫）",
    "visual_scanned_none": "扫了但没有值得留的画面",
    "visual_capture_failed": "画面抓取失败（网络/解码）",
    "digest_empty": "浓缩后无内容（广告空话被删光）",
    "digest_failed": "浓缩失败",
    "apply_failed": "落库失败（digest 已留，可补落）",
    "error": "其它错误",
}

# 能不能靠"再跑一次"救回来（补抓时按这个筛）
REASON_RETRYABLE = {
    "subtitle_missing": True,       # AI 字幕可能过几天补全
    "subtitle_low_coverage": True,  # 同上，只生成了一部分
    "subtitle_too_short": True,     # 壳子在内容空，装好 ASR/官方补轨再跑可能成
    "asr_unavailable": True,        # 装好 faster-whisper/模型再跑即可能成
    "asr_failed": True,             # 下载/解码/OOM，重跑常能好
    "asr_no_audio": False,          # 付费/充电专属：重试一百次也还是拿不到音频
    "visual_capture_failed": True,  # CDN 抽风/解码失败，重跑常能好
    "apply_failed": True,           # digest 还在，重落即可
    "digest_failed": True,
    "asr_used": False,              # 已经救回入库了，无需再跑
    "no_speech": False,             # 结论不是故障：视频本来就没有解说
    "subtitle_off_topic": False,    # 轨道本身就是错的，重跑还是同一条
    "subtitle_unusable": False,
    "visual_judged_useless": False, # 内容判定，不是故障
    "visual_scanned_none": False,
    "visual_off": False,
    "digest_empty": False,
    "ok": False,
}

# 画面这一侧"到底发生了什么"，与成败无关，每条都记
VISUAL_VERDICT_LABEL = {
    "kept": "留下画面",
    "scanned_none": "扫了但没留下",
    "no_pages": "判定没页（未扫）",
    "off": "开关关闭",
    "failed": "抓取失败",
    "": "未涉及",
}


def subtitle_reason(why: str) -> str:
    """把字幕闸门那句中文理由归成稳定的机器可读原因。"""
    w = why or ""
    if "不是这条视频" in w or "不像这条视频" in w or "像错轨" in w:
        return "subtitle_off_topic"
    if "覆盖" in w or "%" in w:
        return "subtitle_low_coverage"
    if "没有字幕缓存" in w:
        return "subtitle_missing"
    return "subtitle_unusable"


def classify_reason(status: str, error: str = "", gate: str = "") -> str:
    """老记录没有 reason 字段时，从 status/error 反推一个（兼容历史账）。"""
    txt = f"{error or ''} {gate or ''}"
    if status == "ok":
        return "ok"
    if status == "no_speech":
        return "no_speech"    # ASR 判定全程无语音：结论，不是故障
    if status == "no_subtitle":
        return "subtitle_missing"
    if status == "no_usable_subtitle":
        if "dash.audio" in txt:
            return "asr_no_audio"   # 付费/充电专属，不是故障
        return subtitle_reason(txt)
    if status == "scan_failed":
        return "visual_capture_failed"
    if status == "digest_failed":
        return "digest_empty" if ("没有产出" in txt or "什么都不剩" in txt) else "digest_failed"
    if status == "apply_failed":
        return "apply_failed"
    return "error"


def is_retryable(reason: str) -> bool:
    return bool(REASON_RETRYABLE.get(reason, False))
