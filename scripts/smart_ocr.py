# -*- coding: utf-8 -*-
"""智能 OCR CLI：本地二分类 + GLM 4.6/5 双模型 + 独立 md 输出。

用法:
    python scripts/smart_ocr.py --dir <图片文件夹> [--output <结果文件名>] [--dry-run]
    python -m backend.main ocr-smart --dir <图片文件夹> [--output <结果文件名>] [--dry-run]

说明:
    --dry-run 只做本地复杂度分类，不调用 GLM API，也不写输出文件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.ocr.smart_ocr import run_smart_ocr


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="智能 OCR：本地分类 + GLM 双模型 + 独立 md 输出")
    p.add_argument("--dir", required=True, help="图片文件夹路径")
    p.add_argument("--output", default=None,
                   help="输出 md 文件名；缺省为 <dir>/ocr_result.md；相对路径按 <dir> 解析")
    p.add_argument("--dry-run", action="store_true",
                   help="只做本地复杂度分类，不调用 GLM API，不写输出文件")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_smart_ocr(args.dir, args.output, args.dry_run)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
