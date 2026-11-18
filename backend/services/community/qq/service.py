r"""QQ pipeline 正式封装。

设计原则：
1. 不复制登录态，不绕过 NapCat/QCE。
2. 不依赖 `D:\QQChatExporter\chathistory` 下的程序；流水线代码已迁入 infosystem。
3. 不直接写 `消息.tsv`；只调用迁入后的 export.py / archive.py / analyze.py / kb.py。
4. API 默认只暴露只读能力；写操作必须走显式 CLI 确认。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.config.settings import Settings


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int
    output: str
    timeout: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timeout


class QQPipelineService:
    """封装 QQChatExporter/chathistory 的已验证命令。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.code_root = Path(self.settings.qq_pipeline_code_root).resolve()
        self.data_root = Path(self.settings.qq_pipeline_data_root).resolve()

    def _validate(self):
        if not self.code_root.is_dir():
            raise FileNotFoundError(f"QQ pipeline 代码目录不存在: {self.code_root}")
        for name in ("export.py", "archive.py", "analyze.py", "kb.py"):
            if not (self.code_root / name).is_file():
                raise FileNotFoundError(f"QQ pipeline 缺少入口: {self.code_root / name}")
        self.data_root.mkdir(parents=True, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["QQ_PIPELINE_DATA_DIR"] = str(self.data_root)
        env["QQ_PIPELINE_CODE_DIR"] = str(self.code_root)
        env["QQ_EXPORT_DIR"] = str(Path(os.environ.get("QQ_EXPORT_DIR", r"C:\Users\HP\Documents\QQChatExporter\exports")))
        return env

    def run(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        capture: bool = True,
    ) -> CommandResult:
        self._validate()
        command = [sys_executable(), *args]
        timeout = timeout or self.settings.qq_readonly_timeout_seconds
        try:
            process = subprocess.run(
                command,
                cwd=str(self.code_root),
                env=self._environment(),
                capture_output=capture,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            output = (process.stdout or "") + (process.stderr or "")
            return CommandResult(command, str(self.code_root), process.returncode, output)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            output += (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return CommandResult(command, str(self.code_root), 124, output + "\n命令超时", timeout=True)

    def export_status(self) -> CommandResult:
        return self.run(["export.py", "--config", str(self.settings.qq_export_config), "--status"])

    def export_dry_run(self, only: str | None = None, overlap: float | None = None) -> CommandResult:
        args = ["export.py", "--config", str(self.settings.qq_export_config), "--dry-run"]
        if only:
            args += ["--only", only]
        if overlap is not None:
            args += ["--overlap", str(overlap)]
        return self.run(args)

    def archive_list(self) -> CommandResult:
        return self.run(["archive.py", "list"])

    def analyze_plan(self, group: str) -> CommandResult:
        return self.run(["analyze.py", "plan", "--group", group])

    def analyze_status(self, group: str) -> CommandResult:
        return self.run(["analyze.py", "status", "--group", group])

    def kb_check(self, group: str) -> CommandResult:
        return self.run(["kb.py", "check", "--group", group])

    def prune_missing(
        self,
        group: str,
        *,
        batch: str | None = None,
        dry_run: bool = True,
        note: str | None = None,
    ) -> CommandResult:
        args = ["archive.py", "prune-missing", "--group", group]
        if batch:
            args += ["--batch", batch]
        if dry_run:
            args.append("--dry-run")
        if note:
            args += ["--note", note]
        return self.run(args, timeout=1800)

    def analyze_run(
        self,
        group: str,
        *,
        limit: int | None = None,
        only: str | None = None,
        dry_run: bool = False,
        model: str | None = None,
        prompt: str | None = None,
        no_media_check: bool = False,
    ) -> CommandResult:
        args = ["analyze.py", "run", "--group", group]
        if limit is not None:
            args += ["--limit", str(limit)]
        if only:
            args += ["--only", only]
        if dry_run:
            args.append("--dry-run")
        if model:
            args += ["--model", model]
        if prompt:
            args += ["--prompt", prompt]
        if no_media_check:
            args.append("--no-media-check")
        return self.run(args, timeout=max(self.settings.qq_export_timeout_seconds, 7200))

    def kb_add(
        self,
        group: str,
        *,
        dry_run: bool = False,
        model: str | None = None,
        per_call: int | None = None,
        batches: str | None = None,
        rebuild: bool = False,
    ) -> CommandResult:
        args = ["kb.py", "add", "--group", group]
        if dry_run:
            args.append("--dry-run")
        if model:
            args += ["--model", model]
        if per_call is not None:
            args += ["--per-call", str(per_call)]
        if batches:
            args += ["--batches", batches]
        if rebuild:
            args.append("--rebuild")
        return self.run(args, timeout=max(self.settings.qq_export_timeout_seconds, 7200))

    def run_incremental_tests(self) -> CommandResult:
        return self.run(["_tools/_test_incremental.py"], timeout=300)

    def diag_media(self) -> CommandResult:
        return self.run(["_tools/_diag_media.py"], timeout=300)

    def diag_duplicate_media(self) -> CommandResult:
        return self.run(["_tools/_diag_dupmedia.py"], timeout=300)

    def export(
        self,
        *,
        launch: bool = False,
        import_to_archive: bool = True,
        only: str | None = None,
        overlap: float | None = None,
        full: bool = False,
    ) -> CommandResult:
        """写操作：导出并可选自动入库。调用方必须显式确认。"""
        args = ["export.py", "--config", str(self.settings.qq_export_config)]
        if launch:
            args.append("--launch")
        if not import_to_archive:
            args.append("--no-import")
        if only:
            args += ["--only", only]
        if overlap is not None:
            args += ["--overlap", str(overlap)]
        if full:
            args.append("--full")
        return self.run(
            args,
            timeout=self.settings.qq_export_timeout_seconds,
            capture=False,
        )

    def verify(self, groups: list[str] | None = None, data_root: str | Path | None = None) -> dict:
        """集成接口 §5 的五条判据；只读/自检，不调 LLM。"""
        groups = groups or ["27贝壳校招互帮互助群", "AI Agent 综合交流"]
        original_data_root = self.data_root
        if data_root:
            self.data_root = Path(data_root).resolve()
        try:
            checks = {
                "archive_list": self.archive_list(),
                "roundtrip": self.run(["_tools/_diag_rt.py"], timeout=300),
                "media": self.diag_media(),
                "incremental_tests": self.run_incremental_tests(),
            }
            kb = {group: self.kb_check(group) for group in groups}
            # 光看退出码不够：archive.py list / _diag_rt.py / _diag_media.py 都是"只打印、恒 exit 0"，
            # 所以必须**解析它们的输出**才能真正判定（2026-10-18 实测：把 total 改大 5、媒体全删的
            # 数据根，只看退出码时 all_ok 仍为 True）。
            assertions = {
                "counts_match_total": _assert_counts_equal(self.data_root, checks["archive_list"].output),
                "roundtrip_identical": _assert_roundtrip(checks["roundtrip"].output),
                "media_diffs_zero": _assert_media_zero(checks["media"].output),
            }
            result = {
                "code_root": str(self.code_root),
                "data_root": str(self.data_root),
                "checks": {name: item.__dict__ for name, item in checks.items()},
                "kb_checks": {group: item.__dict__ for group, item in kb.items()},
                "assertions": assertions,
                "all_ok": all(item.ok for item in checks.values())
                and all(item.ok for item in kb.values())
                and all(assertions.values()),
            }
            return result
        finally:
            self.data_root = original_data_root


_COUNT_RE = re.compile(r"消息\s+(\d+)\s+条")


def _assert_counts_equal(data_root: Path, list_output: str) -> bool:
    """判据 1：`archive.py list` 现算的总条数必须 == 所有群 `_meta.json` 的 total 之和（R45 口径）。"""
    listed = sum(int(m.group(1)) for m in _COUNT_RE.finditer(list_output or ""))
    if not listed:
        return False
    import json

    recorded = 0
    try:
        for meta_fp in Path(data_root).glob("*/_meta.json"):
            recorded += int(json.loads(meta_fp.read_text(encoding="utf-8")).get("total") or 0)
    except Exception:
        return False
    return listed == recorded


def _assert_roundtrip(output: str) -> bool:
    """判据 2：`_diag_rt.py` 必须报"完全一致=True"，且不允许出现 False。"""
    text = output or ""
    return "完全一致=True" in text and "完全一致=False" not in text


def _assert_media_zero(output: str) -> bool:
    """判据 3：`_diag_media.py` 的"引用得到但档案里没有 / 盘上有但没人引用"必须都是 0。"""
    text = output or ""
    seen = 0
    for line in text.splitlines():
        if "=" not in line:
            continue
        if "引用得到但档案里没有" in line or "盘上有但没人引用" in line:
            seen += 1
            tail = line.rsplit("=", 1)[1].strip()
            if not tail.startswith("0"):
                return False
    return seen > 0


def sys_executable() -> str:
    import sys
    return sys.executable
