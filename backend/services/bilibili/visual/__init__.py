"""B站画面要素提取（问题3）：视频≈PPT，最少帧集合覆盖最大画面信息量。

管线：字幕LLM预判 → 窗口化低清扫描 → 结构去重 → 低清OCR+页判 → 高清重拉 → 终OCR。
出处与实验记录见 test/bili_scan_exp/{HANDOFF,REPORT}.md（2026-10-30 起沙盒，2026-11-05 合并）。
"""
from .paths import SUB_DIR, VISUAL_ROOT
