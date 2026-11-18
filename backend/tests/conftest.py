"""kb 测试公共设施：marker 注册、快照对比、真实数据判定。

快照语义：首次运行自动落盘（作为基线）；此后每次运行做**严格对比**，
不一致即 P0 失败。确需刷新基线时显式设置环境变量 `KB_UPDATE_SNAPSHOT=1`
（用于经批准的真相源变更，例如 M2 tag 清洗 / M3 D32 组织标记落盘）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = PROJECT_ROOT / "knowledgebase" / "_meta" / "reports" / "test_reports"


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: 合成语料单元测试（默认）")
    config.addinivalue_line("markers", "real: 真实数据集成测试，只读")
    config.addinivalue_line("markers", "slow: 单次运行 >30s")


@pytest.fixture(autouse=True)
def _disable_rerank(monkeypatch):
    """D55：单测默认关闭交叉编码器重排。

    否则每个 ask() 都会加载 2.14 GB 模型并改变召回池（top_k×3），
    让"合成语料"的单测既慢又不可复现。需要测重排的用例显式传
    `rerank=True, rerank_scorer=...`（注入打分函数后不受本开关影响）。
    """
    monkeypatch.setenv("KB_RERANK", "0")


def assert_snapshot(name: str, current: dict | list) -> None:
    """与 `knowledgebase/_meta/reports/test_reports/{name}.json` 严格对比；缺失则落盘为基线。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.json"
    payload = json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2)
    if not path.exists() or os.environ.get("KB_UPDATE_SNAPSHOT") == "1":
        path.write_text(payload + "\n", encoding="utf-8", newline="")
        pytest.skip(f"快照基线已写入 {path.name}（本次不作为断言）")
    expected = path.read_text(encoding="utf-8")
    if expected != payload + "\n":
        # 给出可读差异（只报首个不同条目），避免整份 json 刷屏
        try:
            want = json.loads(expected)
            detail = _first_difference(want, current)
        except json.JSONDecodeError:
            detail = "既有快照不是合法 JSON"
        raise AssertionError(
            f"{name} 与基线快照不一致：{detail}\n"
            f"（如属经批准的真相源变更，用 KB_UPDATE_SNAPSHOT=1 显式刷新）"
        )


def _first_difference(want, got, trail: str = "") -> str:
    if type(want) is not type(got):
        return f"{trail or '<root>'}: 类型 {type(want).__name__} -> {type(got).__name__}"
    if isinstance(want, dict):
        for key in sorted(set(want) | set(got)):
            if key not in got:
                return f"{trail}.{key}: 基线有而现状缺失"
            if key not in want:
                return f"{trail}.{key}: 现状新增 {got[key]!r}"
            if want[key] != got[key]:
                return _first_difference(want[key], got[key], f"{trail}.{key}")
        return "无差异（应为相等）"
    if isinstance(want, list):
        if len(want) != len(got):
            return f"{trail}: 长度 {len(want)} -> {len(got)}"
        for index, (left, right) in enumerate(zip(want, got)):
            if left != right:
                return _first_difference(left, right, f"{trail}[{index}]")
        return "无差异（应为相等）"
    return f"{trail}: {want!r} -> {got!r}"


def tree_fingerprint(root: Path, *, digest: bool = True) -> dict:
    """目录指纹：(相对路径, size, mtime_ns, md5[:16]) 列表。

    真相源与旧索引都靠它做"没被动过"的硬证据。
    """
    entries = []
    if not root.exists():
        return {"root": str(root), "exists": False, "files": []}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        record = {
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if digest:
            record["md5_16"] = hashlib.md5(path.read_bytes()).hexdigest()[:16]
        entries.append(record)
    return {"root": str(root), "exists": True, "count": len(entries), "files": entries}


def file_fingerprint(path: Path) -> dict:
    """单文件指纹（I10 用于 `ragsearch/registry.json`）。

    与 `tree_fingerprint` 的两点差异，都是为了不当"狼来了"：
    1. **不记 mtime**：配置语义只看内容，`git checkout` / CRLF 转换不该触发红灯；
    2. **换行归一化**（CRLF→LF）后再算 md5：本仓库 LF/CRLF 混存，git 触文件时
       "LF will be replaced by CRLF" 会让字节变但 JSON 语义没变。
    """
    if not path.exists():
        return {"path": str(path), "exists": False}
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return {
        "path": str(path),
        "exists": True,
        "size": len(raw),
        "md5_16": hashlib.md5(raw).hexdigest()[:16],
    }
