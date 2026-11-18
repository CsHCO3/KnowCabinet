"""批量回刷：把"试过但没进库"的视频用 ASR 兜底重跑一遍（串行、逐条子进程、超时杀掉）。

为什么要有这个东西（而不是直接 bili-visual-scan 十几个 BV 号）：
- 单条超时：实测有视频的 view 接口偶发残缺，抓字幕会挂好几分钟，一条卡住不能拖死整批
- 逐条落盘：日志一视频一份，出问题直接看是哪一步
- 断点续跑：progress.jsonl 记着跑过谁，崩了重启不会从头发车
- 默认不重跑"结论类"失败：`no_speech` 是无解说的结论，重跑只是白烧 GPU

刻意串行：6GB 卡上 whisper(3.3GB) 与 reranker(1.2GB) 同时驻留会 OOM，端点并发也会被重置。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .reasons import is_retryable
from .visual.paths import PROJECT_ROOT

RUNS = PROJECT_ROOT / "data" / "bilibili" / "visual" / "_runs.jsonl"
WORK = PROJECT_ROOT / "data" / "bilibili" / "backfill"
LOGS = WORK / "logs"
PROGRESS = WORK / "progress.jsonl"

DEFAULT_TIMEOUT = 2400          # 单条 40 分钟：30 分钟片的 ASR ≈ 13 分钟 + 管线
_FAKE = {"BV1zzzzzzzzz"}        # 历史上手写过的测试号，别浪费 ASR 时间去跑它
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


def latest_runs() -> dict:
    """每个 bvid 的最后一条记录（口径同 bili-status）。"""
    last: dict[str, dict] = {}
    if not RUNS.exists():
        return last
    for line in RUNS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("bvid"):
            last[d["bvid"]] = d
    return last


def targets(last: dict | None = None, *, retry_failed: bool = False) -> list[str]:
    """没进库的算靶子，但排除两类白跑的：

    - `no_speech`：VAD 判定全程无解说的结论，重跑还是同一条音频
    - `digest_empty` / `visual_judged_useless` 这类"内容判定"失败（is_retryable=False）
      默认也不跑，除非 retry_failed 显式打开

    排序先短片后长片：快出结果，长片留在后面磨。
    """
    last = latest_runs() if last is None else last
    rows = []
    for bv, d in last.items():
        if (d.get("status") or "ok") == "ok":
            continue
        if bv in _FAKE or not _BV_RE.match(bv or ""):
            continue
        reason = d.get("reason") or ""
        if reason == "no_speech":
            continue                      # 结论，不是故障
        if not retry_failed and not is_retryable(reason):
            continue                      # 内容判定失败，重跑没有意义
        rows.append((int(d.get("elapsed") or 0), bv))
    rows.sort()
    return [bv for _, bv in rows]


def done_set(progress: Path = PROGRESS) -> set[str]:
    if not progress.exists():
        return set()
    out = set()
    for line in progress.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["bvid"])
        except Exception:
            pass
    return out


def run_one(bv: str, timeout: int = DEFAULT_TIMEOUT,
            python_exe: str = r"D:/Anaconda/envs/infosystem/python.exe",
            logs: Path = LOGS) -> dict:
    """起子进程跑一条；输出实时写 logs/<bv>.log，超时就杀，返回一行账。"""
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{bv}.log"
    t0 = time.time()
    with log_path.open("w", encoding="utf-8") as f:
        p = subprocess.Popen(
            [python_exe, "-u", "-m", "backend.main", "bili-visual-scan", bv,
             "--asr-fallback", "--no-index"],
            cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
        assert p.stdout is not None
        try:
            for line in p.stdout:
                f.write(line)
                f.flush()
                if time.time() - t0 > timeout:
                    p.kill()
                    f.write(f"\n[backfill] 超时 {timeout}s，已杀掉\n")
                    break
        finally:
            p.wait()
    return {"ts": time.strftime("%Y-%m-%d %H:%M"), "bvid": bv,
            "exit": p.returncode, "elapsed": round(time.time() - t0, 1)}


def run(only: list[str] | None = None, *, retry_failed: bool = False,
        timeout: int = DEFAULT_TIMEOUT, no_index: bool = False,
        python_exe: str = r"D:/Anaconda/envs/infosystem/python.exe",
        dry: bool = False) -> list[dict]:
    """跑一批；返回每条的账。dry=True 只算靶子不执行。"""
    last = latest_runs()
    todo = list(only) if only else targets(last, retry_failed=retry_failed)
    done = done_set()
    todo = [b for b in todo if b not in done]
    print(f"靶子 {len(todo)} 条（已完成 {len(done)} 条）")
    for bv in todo:
        d = last.get(bv) or {}
        print(f"  {bv}  {d.get('status')}  上次 {d.get('elapsed')}s  "
              f"{(d.get('error') or '')[:50]}")
    if dry:
        return []

    out = []
    for i, bv in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {bv}", flush=True)
        rec = run_one(bv, timeout, python_exe)
        out.append(rec)
        PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        with PROGRESS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  exit={rec['exit']} 用时 {rec['elapsed']}s", flush=True)

    if not no_index and todo:
        # 索引只做一次：每条 --no-index 是为了省掉 N 次全库重建（每次约 2 分钟）
        for cmd in ("kb-scan", "kb-embed"):
            print(f"\n索引: {cmd} ...", flush=True)
            t = time.time()
            subprocess.run([python_exe, "-m", "backend.main", cmd],
                           cwd=str(PROJECT_ROOT))
            print(f"  {cmd} 完成 ({time.time() - t:.0f}s)", flush=True)
    return out
