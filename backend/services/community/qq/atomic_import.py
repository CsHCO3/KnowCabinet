"""把 QQ 已有知识库 Markdown 确定性转换成统一原子知识点 JSONL。

 purposely does not use LLM, does not read screenshots, and does not require
 original chat evidence. Source markers are preserved for later evidence lookup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.path_setup import PROJECT_ROOT


CODE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = (
    PROJECT_ROOT / "data" / "community" / "qq" / "pipeline"
    / "AI Agent 综合交流" / "提炼" / "知识库"
)
DEFAULT_OUT = PROJECT_ROOT / "data" / "community" / "qq" / "knowledge" / "atomic_entries.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "data" / "community" / "qq" / "knowledge" / "atomic_import_report.json"

ENTRY_RE = re.compile(r"^-\s*\*\*(.+?)\*\*\s*[：:]\s*(.+)$")
MARKER_RE = re.compile(r"(?<![A-Za-z0-9])(\d{3}[A-Z]\d+)(?![A-Za-z0-9])")
LINE_REF_RE = re.compile(r"行#(\d+)")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _kind(markers: list[str]) -> str:
    letters = {re.match(r"^\d{3}([A-Z])\d+$", marker).group(1) for marker in markers if re.match(r"^\d{3}[A-Z]\d+$", marker)}
    if "A" in letters:
        return "risk_or_ad"
    if "O" in letters:
        return "media_pointer"
    if letters <= {"K", "J"}:
        return "knowledge"
    return "knowledge"


def import_markdown(source_dir: Path, output: Path, report_path: Path) -> dict:
    source_dir = source_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    file_reports: list[dict] = []
    skipped: list[dict] = []
    seen_exact: set[str] = set()
    exact_duplicates = 0

    files = sorted(
        path for path in source_dir.glob("*.md")
        if path.name not in {"_索引.md", "原文留存.md"}
    )
    for path in files:
        match = re.match(r"^\d+-(.+)\.md$", path.name)
        theme = match.group(1) if match else path.stem
        text = path.read_text(encoding="utf-8")
        file_entries = 0
        file_skipped = 0

        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ">")):
                continue
            if not stripped.startswith("- "):
                continue
            match = ENTRY_RE.match(stripped)
            if not match:
                file_skipped += 1
                skipped.append({
                    "file": path.name,
                    "line": line_no,
                    "reason": "bullet did not match object/statement format",
                    "text": stripped[:300],
                })
                continue

            object_name = match.group(1).strip()
            full_statement = match.group(2).strip()
            # 028A1 这类记号偶尔会写在对象名里，所以从整条 bullet 提取。
            markers = MARKER_RE.findall(stripped)
            line_refs = [int(x) for x in LINE_REF_RE.findall(stripped)]
            statement = re.sub(
                r"\s*[（(]\s*\d{3}[A-Z]\d+(?:[、，,]\s*[^）()]*)?[）)]\s*$",
                "",
                full_statement,
            ).strip() or full_statement

            fingerprint = _sha16(f"{theme}\n{object_name}\n{statement}")
            if fingerprint in seen_exact:
                exact_duplicates += 1
                continue
            seen_exact.add(fingerprint)

            file_entries += 1
            entries.append({
                "id": f"qq-{len(entries) + 1:06d}",
                "source": "qq",
                "theme": theme,
                "kind": _kind(markers),
                "object": object_name,
                "statement": statement,
                "source_markers": markers,
                "source_line_refs": line_refs,
                "source_file": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "evidence_status": "not_imported",
                "statement_hash": fingerprint,
                "imported_at": _now(),
            })

        file_reports.append({
            "file": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "theme": theme,
            "entries": file_entries,
            "skipped": file_skipped,
        })

    tmp = output.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(output)

    report = {
        "generated_at": _now(),
        "source_dir": str(source_dir),
        "output": str(output),
        "files_scanned": len(files),
        "entries_imported": len(entries),
        "exact_duplicates_skipped": exact_duplicates,
        "bullets_skipped": len(skipped),
        "missing_source_markers": sum(1 for entry in entries if not entry["source_markers"]),
        "evidence": "not_imported_by_design",
        "files": file_reports,
        "skipped_lines": skipped,
    }
    tmp_report = report_path.with_suffix(".json.tmp")
    tmp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp_report.replace(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Import existing QQ knowledge Markdown as atomic JSONL")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    report = import_markdown(args.source_dir, args.output, args.report)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps({
        "files_scanned": report["files_scanned"],
        "entries_imported": report["entries_imported"],
        "exact_duplicates_skipped": report["exact_duplicates_skipped"],
        "bullets_skipped": report["bullets_skipped"],
        "missing_source_markers": report["missing_source_markers"],
        "output": report["output"],
        "report": report["report_path"] if "report_path" in report else str(args.report),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
