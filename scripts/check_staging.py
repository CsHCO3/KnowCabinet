#!/usr/bin/env python
"""提交前卫生检查 —— 本机数据 / 模型 / 登录态 / 账号真值一律不许进仓库。

三种用法：
  python scripts/check_staging.py            # 检查暂存区（pre-commit 钩子用，默认）
  python scripts/check_staging.py --audit    # 检查整个索引（定期体检，看仓库里已经有什么）
  python scripts/check_staging.py --paths a b c   # 检查指定路径（手工排查用）

为什么要有它：2026-10-27 的教训是「把运行时数据写在包目录，只靠 .gitignore 拦」——
.gitignore 少写一条，cookie 就跟着代码提交出去了（`papers/*_session/storage_state.json`
带着知网 52 个 cookie 在 GitHub 上躺了一个多月）。.gitignore 是「声明」，这个脚本是
「执行」，两道都要有；再加一条结构约束：可写运行时数据只准放 data/。

退出码：0 干净；1 有违规（打印清单 + 修法）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── 路径规则：(正则, 说明) ──────────────────────────────────────────────
# 命中即拦。放行名单在 ALLOW 里单独写。
DENY_PATHS: list[tuple[str, str]] = [
    (r"^data/", "data/ 是本机运行时数据（kb.db、QQ 原始导出、下载的 pdf…）"),
    (r"^knowledgebase/", "knowledgebase/ 是私域知识库本体"),
    (r"^\.models/", "模型权重"),
    (r"^ragsearch/index/", "索引构建产物"),
    (r"_session/", "论文站点登录态目录（含 storage_state.json）"),
    (r"(^|/)storage_state\.json$", "Playwright 登录态，里面是 cookie"),
    (r"(^|/)cookies?\.json$", "cookie 文件"),
    (r"(^|/)export_groups\.json$", "QQ 号 / 群号真值（模板是 export_groups.example.json）"),
    (r"(^|/)\.envs?$", "环境变量 / API key"),
    (r"(^|/)model_config\.json$", "本机模型配置"),
    (r"\.(safetensors|onnx|gguf|pt|pth|ckpt|bin|pkl|db|sqlite|sqlite3)$", "模型权重 / 数据库文件"),
    (r"\.(pdf|docx|doc|xlsx|xls|pptx|zip|rar|7z|mp4|mp3|wav)$", "下载 / 归档的二进制内容"),
]

#: 明确的例外（第三方工具，运行需要；新增请写理由）
ALLOW = {
    "scripts/mmmojo_64.dll": "微信 OCR 工具链的一部分，跑 ocr 需要",
    "scripts/wechat_ocr.exe": "微信 OCR 工具本体，跑 ocr 需要",
    "scripts/wcocr.dll": "微信 OCR 工具本体，跑 ocr 需要",
}

# ── 内容规则：(正则, 说明)，只看文本、只看 < 2MB 的文件 ────────────────
# 注意：cookie 名只在「后面跟着一串字面量」时才算泄漏，`f"BDUSS={bduss}"` 是拼请求头，不算。
DENY_CONTENT: list[tuple[str, str]] = [
    (r'"cookies"\s*:\s*\[', "看起来是 Playwright storage_state / cookie 导出"),
    (r"(JSESSIONID|cf_clearance|BDUSS|CASTGC)\s*[=:]\s*[\"']?[A-Za-z0-9+/%_.\-]{24,}", "硬编码的站点 cookie"),
    (r"sk-[A-Za-z0-9]{20,}", "疑似 API key 明文"),
    (r'"qq_uin"\s*:\s*"\d{5,}"', "QQ 号明文"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
]

MAX_BYTES = 2 * 1024 * 1024

def private_ids() -> dict[str, str]:
    path = REPO / ".envs"
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    config = dotenv_values(path, interpolate=False)
    return {part.strip(): "本地配置的账号或群号" for part in
            (config.get("STAGING_PRIVATE_IDS") or "").split(",") if part.strip()}


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败: {out.stderr.strip()}")
    return out.stdout


def _tracked_in_index() -> list[str]:
    return [p for p in _git("ls-files", "--cached", "-z").split("\0") if p]


def _staged() -> list[str]:
    txt = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in txt.split("\0") if p]


def _blob_bytes(path: str) -> bytes:
    """取索引里的内容（不是工作区），避免检查了工作区却提交了索引。

    不在索引里时（还没 add，或 --paths 手工排查）退回读工作区文件。
    """
    res = subprocess.run(
        ["git", "show", f":{path}"], cwd=REPO, capture_output=True
    )
    if res.returncode == 0:
        return res.stdout
    p = REPO / path
    try:
        return p.read_bytes()
    except OSError:
        return b""


def check(paths: list[str]) -> list[str]:
    ids = private_ids()
    import re

    problems: list[str] = []
    for p in paths:
        posix = p.replace("\\", "/")
        if posix in ALLOW:
            continue
        for rx, why in DENY_PATHS:
            if re.search(rx, posix):
                problems.append(f"  {p}  ← {why}")
                break
        else:
            data = _blob_bytes(p)
            if len(data) > MAX_BYTES:
                problems.append(f"  {p}  ← 超过 {MAX_BYTES // 1024 // 1024}MB（{len(data) // 1024}KB）")
                continue
            if b"\0" in data[:4096]:
                continue  # 二进制，内容规则跳过
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            hit = None
            for s, why in ids.items():
                if s in text:
                    hit = f"  {p}  ← 含{why}（{s}）"
                    break
            if hit is None:
                for rx, why in DENY_CONTENT:
                    if re.search(rx, text):
                        hit = f"  {p}  ← 内容命中：{why}"
                        break
            if hit:
                problems.append(hit)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="仓库卫生检查")
    ap.add_argument("--audit", action="store_true", help="检查整个索引（不只是暂存区）")
    ap.add_argument("--paths", nargs="*", help="检查指定路径")
    args = ap.parse_args()

    if args.paths:
        targets = args.paths
    elif args.audit:
        targets = _tracked_in_index()
    else:
        targets = _staged()

    problems = check(targets)
    if not problems:
        return 0

    scope = "索引" if args.audit else "暂存区"
    print(f"\n✗ {len(problems)} 处不该入库的东西（检查范围：{scope}）：\n")
    print("\n".join(problems))
    print(
        "\n修法：\n"
        "  · 已经 add 的：git rm --cached <路径>（文件留在磁盘上，只是不再跟踪）\n"
        "  · 让 git 以后别再看到它：往 .gitignore 加一条，或在 DENY_PATHS 里补规则\n"
        "  · 确实要放行：在 scripts/check_staging.py 的 ALLOW 里加一行并写理由\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
