#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R46 导出自动化：把「登录 QQ → 选群聊 → 选参数 → 点导出 → 等落盘」全交给脚本。

为什么不点界面：QCE 前端本身就是调 REST 接口（qce-server.exe，127.0.0.1:40653），
脚本直接调同一套接口，不动窗口、不认坐标、不怕 UI 改版。

用法（都在 D:\\QQChatExporter\\chathistory 下跑）：
    python export.py --status              # 只看：服务通不通 / 登录没登录 / 每群下次从哪天导
    python export.py --dry-run             # 只看：打算用哪些参数、导哪段时间（不叫 API）
    python export.py                       # 导出 + 自动 archive.py import
    python export.py --no-import           # 只导出，入库自己另跑
    python export.py --launch              # 40653 不通时自动拉起 launcher-user.bat 并快速登录
    python export.py --only 校招            # 只导名字里含"校招"的群
    python export.py --overlap 30          # 临时改重叠分钟数（默认读 export_groups.json）

设计约束（踩过才知道的，别改）：
  * accessToken 文件位置**随启动方式变**（QQ Chat Exporter.exe → .qce-config\\；
    launcher-user.bat → ~\\.qq-chat-exporter\\），所以令牌必须读运行日志里最后一条
    "[SecurityManager] config:" 现取，不许写死路径（实测踩过：写死 → 403 无效令牌）。
  * options.outputDir 只允许 exports\\ 和 scheduled-exports\\ 两个根，别处会 400。
  * 导出必须**先跑完**再 import：exporter 的 resources\\ 是流水线的"资源专用文件夹"，
    同一目录，交叉着来会搬错文件（R41/R42 那条闸门管的就是这个顺序）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
_PARENTS = CODE_DIR.parents
_PROJECT = _PARENTS[4] if len(_PARENTS) > 4 and _PARENTS[4].name == "infosystem" else CODE_DIR
_DEFAULT_DATA = _PROJECT / "data" / "community" / "qq" / "pipeline"
DATA_DIR = Path(os.environ.get("QQ_PIPELINE_DATA_DIR", str(_DEFAULT_DATA))).resolve()
DEFAULT_CFG = CODE_DIR / "export_groups.json"

# 导出请求里那些"界面不会给你改"的固定项（照抄 tasks.jsonl 里的历史原文）
BASE_OPTIONS = {
    "batchSize": 5000,
    "includeResourceLinks": True,
    "includeSystemMessages": False,
    "prettyFormat": True,
    "exportAsZip": False,
    "embedAvatarsAsBase64": False,
    "embedResourcesAsDataUri": False,
    "preferGroupMemberName": True,
    "debugExport": False,
}
TERMINAL = ("completed", "failed", "cancelled")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ts_now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def fmt_local(sec: float) -> str:
    return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")


def die(msg: str, code: int = 2) -> "NoReturn":  # noqa: F821
    say(f"✗ {msg}")
    raise SystemExit(code)


# --------------------------------------------------------------------------- 配置

def load_cfg(path: Path) -> dict:
    if not path.is_file():
        die(f"找不到配置文件 {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("qce_root", r"D:\QQChatExporter")
    cfg.setdefault("qce_port", 40653)
    cfg.setdefault("napcat_port", 6099)
    cfg.setdefault("overlap_minutes", 10)
    cfg.setdefault("task_timeout_seconds", 1800)
    cfg.setdefault("launcher", str(Path(cfg["qce_root"]) / "launcher-user.bat"))
    if not cfg.get("groups"):
        die("配置里没有 groups")
    return cfg


# --------------------------------------------------------------------------- HTTP

def http_json(url: str, method: str = "GET", payload=None, token: str = "",
              timeout: float = 30) -> dict:
    """统一返回 dict：真正的接口返回，或 {"_error": ..., "_status": ...}。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Access-Token"] = token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            out = json.loads(body)
        except Exception:
            out = {"_body": body[:400]}
        out["_status"] = e.code
        return out
    except Exception as e:                      # 连接被拒 / 超时 / 端口没开
        return {"_error": str(e)}


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


# --------------------------------------------------------------------------- 令牌

def token_candidates(log_path: Path) -> list[Path]:
    """运行日志里最后一条 [SecurityManager] config: 才是**正在跑**的那个 server 用的文件。"""
    out: list[Path] = []
    if log_path.is_file():
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.search(r"SecurityManager\] config:\s*(.+\.json)\s*$", line)
                if m:
                    p = Path(m.group(1).strip())
                    if p not in out:
                        out.append(p)
        except OSError:
            pass
    # 兜底：两种启动方式各自的默认位置
    home = Path.home() / ".qq-chat-exporter" / "security.json"
    for p in (home, Path(r"D:\QQChatExporter\.qce-config\security.json")):
        if p not in out:
            out.append(p)
    return list(reversed(out))                  # 最近的优先


def read_token(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("accessToken") or ""
    except Exception:
        return ""


def find_working_token(cfg: dict, soft: bool = False) -> tuple[str, str]:
    """返回 (token, 来源文件)；通不通由 /api/system/info 说了算。soft=True 时失败返回空串。"""
    base = f"http://127.0.0.1:{cfg['qce_port']}"
    tried = []
    for p in token_candidates(Path(cfg["qce_root"]) / "logs" / "qce-runtime.log"):
        tok = read_token(p)
        if not tok:
            tried.append(f"{p}(读不到)")
            continue
        r = http_json(base + "/api/system/info", token=tok, timeout=8)
        if r.get("success"):
            return tok, str(p)
        tried.append(f"{p}({r.get('_status') or r.get('_error') or '失败'})")
    if soft:
        return "", "；".join(tried)
    die("QCE 接口连不上或令牌无效：\n  " + "\n  ".join(tried) +
        "\n  先手工 `launcher-user.bat` 启动并登录 QQ，或加 --launch")


# --------------------------------------------------------------------------- NapCat 登录

def napcat_credential(cfg: dict) -> str:
    webui = Path(cfg["qce_root"]) / "config" / "webui.json"
    try:
        with open(webui, "r", encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("token") or ""
    except Exception as e:
        die(f"读不到 NapCat WebUI 令牌 {webui}: {e}")
    h = hashlib.sha256((raw + ".napcat").encode("utf-8")).hexdigest()
    r = http_json(f"http://127.0.0.1:{cfg['napcat_port']}/api/auth/login",
                  "POST", {"hash": h}, timeout=15)
    cred = ((r.get("data") or {}) or {}).get("Credential") if r.get("code") == 0 else None
    if not cred:
        die(f"NapCat WebUI 登录失败: {r}")
    return cred


def napcat_login_state(cfg: dict, cred: str) -> bool:
    r = http_json(f"http://127.0.0.1:{cfg['napcat_port']}/api/QQLogin/CheckLoginStatus",
                  "POST", {}, token=cred, timeout=15)
    return bool((r.get("data") or {}).get("isLogin"))


def ensure_online(cfg: dict, launch: bool) -> None:
    """保证 40653 通 + QQ 在线；必要时拉起 launcher 并快速登录（免扫码）。"""
    qce = f"http://127.0.0.1:{cfg['qce_port']}"
    if port_open(cfg["qce_port"]):
        say("· QCE 接口已在运行（40653）")
        return
    if not launch:
        die("QCE 接口没起来（40653 不监听）。QQ 没登录，插件就不会起这个服务。\n"
            "  手工跑 `launcher-user.bat` 并登录，或者加 --launch 让脚本自己拉。")

    launcher = Path(cfg["launcher"])
    if not launcher.is_file():
        die(f"找不到启动器 {launcher}")
    say(f"· 拉起 {launcher.name}（QQ + NapCat + QCE）…")
    subprocess.Popen(["cmd", "/c", str(launcher)], cwd=str(launcher.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    say(f"· 等 NapCat WebUI（{cfg['napcat_port']}）…")
    t0 = time.time()
    while time.time() - t0 < 180:
        if port_open(cfg["napcat_port"]):
            break
        time.sleep(1)
    else:
        die("等不到 NapCat WebUI，QQ 可能没起来")

    cred = napcat_credential(cfg)
    if napcat_login_state(cfg, cred):
        say("· QQ 已在线（上次的登录态还在）")
    else:
        uin = str(cfg.get("qq_uin") or "")
        say(f"· QQ 在扫码界面 → 走快速登录（uin={uin}，不用扫码）…")
        r = http_json(f"http://127.0.0.1:{cfg['napcat_port']}/api/QQLogin/SetQuickLogin",
                      "POST", {"uin": uin}, token=cred, timeout=60)
        if r.get("code") != 0:
            die(f"快速登录失败: {r.get('message') or r}")
        t0 = time.time()
        while time.time() - t0 < 120:
            if napcat_login_state(cfg, cred):
                say(f"· QQ 已登录（用时 {time.time() - t0:.0f}s）")
                break
            time.sleep(2)
        else:
            die("快速登录没等到在线状态；可能要在手机上确认")

    say(f"· 等 QCE 插件起服务（{cfg['qce_port']}）…")
    t0 = time.time()
    while time.time() - t0 < 180:
        if port_open(cfg["qce_port"]):
            return
        time.sleep(1)
    die("QQ 登录了但 QCE 服务没起来（看 logs\\qce-runtime.log）")


def qce_info(cfg: dict, token: str) -> dict:
    r = http_json(f"http://127.0.0.1:{cfg['qce_port']}/api/system/info", token=token, timeout=15)
    return (r.get("data") or {}) if r.get("success") else {}


# --------------------------------------------------------------------------- 时间窗口

def last_covered_end_ms(group: str) -> int:
    """该群已覆盖时间轴的最大 end_ms —— 也就是"上次导到哪儿"。"""
    fp = DATA_DIR / group / "_meta.json"
    if not fp.is_file():
        return 0
    try:
        with open(fp, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return 0
    ends = [int(r.get("end_ms") or 0) for r in (meta.get("covered_ranges") or [])]
    return max(ends) if ends else 0


def plan_window(cfg: dict, g: dict, overlap_min: float, full: bool) -> tuple[int, int, str]:
    end_ts = int(time.time())
    last_ms = last_covered_end_ms(g["group"])
    if not last_ms:
        if not full:
            die(f"[{g['group']}] 档案里没有已覆盖时间轴（新群？），全量导出请显式加 --full")
        return 0, end_ts, "全量（无档案）"
    start_ts = int(last_ms / 1000) - int(overlap_min * 60)
    return start_ts, end_ts, f"上次结束 {fmt_local(last_ms / 1000)} − {overlap_min:g} 分钟"


# --------------------------------------------------------------------------- 导出

def export_group(cfg: dict, token: str, g: dict, start_ts: int, end_ts: int) -> dict:
    opts = dict(BASE_OPTIONS)
    opts.update(g.get("options") or {})
    body = {
        "peer": {"chatType": 2, "peerUid": str(g["peer_uid"]), "guildId": ""},
        "sessionName": g.get("session_name") or g["group"],
        "format": "JSON",
        "filter": {"startTime": start_ts, "endTime": end_ts, "includeRecalled": False},
        "options": opts,
    }
    r = http_json(f"http://127.0.0.1:{cfg['qce_port']}/api/messages/export",
                  "POST", body, token=token, timeout=180)
    if not r.get("success"):
        err = (r.get("error") or {}).get("message") or r.get("_error") or r.get("_status")
        return {"ok": False, "error": err, "body": body}
    tid = (r.get("data") or {}).get("taskId")
    return {"ok": True, "task_id": tid, "body": body}


def wait_task(cfg: dict, token: str, task_id: str) -> dict:
    url = f"http://127.0.0.1:{cfg['qce_port']}/api/tasks/{task_id}"
    deadline = time.time() + cfg["task_timeout_seconds"]
    last = None
    while time.time() < deadline:
        r = http_json(url, token=token, timeout=20)
        d = r.get("data") or {}
        st = d.get("status")
        if st != last:
            say(f"    [{ts_now()}] {st} {d.get('progress')}% {str(d.get('message') or '')[:60]}")
            last = st
        if st in TERMINAL:
            return d
        time.sleep(2)
    return {"status": "timeout", "taskId": task_id}


def run_import(json_path: str, note: str) -> bool:
    cmd = [sys.executable, str(CODE_DIR / "archive.py"), "import", "--from", json_path, "--note", note]
    say(f"    入库：python archive.py import --from \"{Path(json_path).name}\"")
    env = dict(**_env_utf8())
    p = subprocess.run(cmd, cwd=str(CODE_DIR), env=env)
    return p.returncode == 0


def _env_utf8() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------------------- 子命令

def cmd_status(cfg: dict, argv_overlap: float | None) -> int:
    say("== QCE 自动导出 · 体检 ==")
    say(f"配置      : {DEFAULT_CFG}")
    say(f"QCE 端口  : {cfg['qce_port']}   {'✔ 在听' if port_open(cfg['qce_port']) else '✗ 没起（QQ 没登录）'}")
    say(f"NapCat    : {cfg['napcat_port']}   {'✔ 在听' if port_open(cfg['napcat_port']) else '✗ 没起'}")
    if port_open(cfg["qce_port"]):
        token, src = find_working_token(cfg, soft=True)
        if not token:
            say(f"令牌/接口 : ✗ {src}")
        else:
            info = qce_info(cfg, token)
            nc = info.get("napcat") or {}
            si = nc.get("selfInfo") or {}
            say(f"令牌来源  : {src}")
            say(f"QQ 在线   : {'✔ ' + str(si.get('uin')) + ' ' + str(si.get('nick')) if nc.get('online') else '✗ 未登录'}")
            say(f"版本      : QCE {info.get('version')}")
    ov = cfg["overlap_minutes"] if argv_overlap is None else argv_overlap
    say("\n== 每群下次窗口 ==")
    for g in cfg["groups"]:
        last = last_covered_end_ms(g["group"])
        if last:
            start = int(last / 1000) - int(ov * 60)
            say(f"  {g['group']:<24} 上次结束 {fmt_local(last / 1000)} → 下次从 {fmt_local(start)} 起")
        else:
            say(f"  {g['group']:<24} 无档案（首次要加 --full）")
        say(f"  {'':<24} 跳过下载: {(g.get('options') or {}).get('skipDownloadResourceTypes')}")
    return 0


def cmd_run(cfg: dict, args) -> int:
    for g in cfg["groups"]:
        say(f"· {g['group']} (peerUid={g['peer_uid']})  跳过下载: "
            f"{(g.get('options') or {}).get('skipDownloadResourceTypes')}")

    if args.dry_run:
        say("\n== dry-run：不会调用任何写接口 ==")
        for g in cfg["groups"]:
            s, e, why = plan_window(cfg, g, args.overlap, args.full)
            say(f"\n[{g['group']}]")
            say(f"  startTime = {s}  ({fmt_local(s)})")
            say(f"  endTime   = {e}  ({fmt_local(e)})")
            say(f"  依据      : {why}")
            opts = dict(BASE_OPTIONS, **(g.get("options") or {}))
            say(f"  options   : {json.dumps(opts, ensure_ascii=False)}")
        return 0

    ensure_online(cfg, args.launch)
    token, src = find_working_token(cfg)
    info = qce_info(cfg, token)
    nc = info.get("napcat") or {}
    si = nc.get("selfInfo") or {}
    if not nc.get("online"):
        die("QCE 起来了但 NapCat 显示未登录（QQ 掉线了？）")
    say(f"· QQ 在线：{si.get('uin')} {si.get('nick')}   令牌：{Path(src).parent.name}")

    results = []
    for g in cfg["groups"]:
        if args.only and args.only not in g["group"]:
            continue
        s, e, why = plan_window(cfg, g, args.overlap, args.full)
        say(f"\n=== [{g['group']}] {fmt_local(s)} → {fmt_local(e)}  （{why}）")
        r = export_group(cfg, token, g, s, e)
        if not r["ok"]:
            say(f"  ✗ 下单失败：{r['error']}")
            results.append((g["group"], "下单失败", r["error"]))
            continue
        say(f"  任务 {r['task_id']} 已创建，等它跑完…")
        d = wait_task(cfg, token, r["task_id"])
        st = d.get("status")
        if st != "completed":
            say(f"  ✗ 任务 {st}：{str(d.get('message') or '')[:120]}")
            results.append((g["group"], st or "失败", str(d.get("message") or "")[:120]))
            continue
        fp = (d.get("filePath") or "").replace("\\\\?\\", "")
        n_msg = d.get("messageCount")
        rs = d.get("resourceSummary") or {}
        say(f"  ✔ 完成：{n_msg} 条消息 / {d.get('fileSize')} 字节 / 资源 attempted={rs.get('attempted')} "
            f"downloaded={rs.get('downloaded')} failed={rs.get('failed')}")
        say(f"    文件：{fp}")
        if args.no_import:
            results.append((g["group"], "已导出（未入库）", fp))
            continue
        if not fp or not Path(fp).is_file():
            say("  ✗ 找不到导出文件，跳过入库")
            results.append((g["group"], "文件缺失", fp))
            continue
        note = f"R46 自动导出（{(g.get('note') or '')[:40]}）"
        ok = run_import(fp, note)
        results.append((g["group"], "已入库" if ok else "入库失败", fp))

    say("\n== 汇总 ==")
    for name, st, extra in results:
        say(f"  {st:<14} {name}   {Path(extra).name if extra and Path(extra).suffix == '.json' else extra}")
    bad = [r for r in results if r[1] not in ("已入库", "已导出（未入库）")]
    if bad:
        say(f"\n✗ {len(bad)} 个群没成功")
        return 1
    if not args.no_import:
        say("\n下一步（③④⑤ 仍需你决定）：")
        say("  python archive.py list                     # 看媒体对账，有缺文件就先 prune-missing（R42 闸门）")
        say("  python analyze.py plan --group 群名        # 切批预览")
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import argparse
    p = argparse.ArgumentParser(
        prog="export.py",
        description="R46：自动导出 QQ 群聊（QCE HTTP API）→ 直接接 archive.py import")
    p.add_argument("--config", default=str(DEFAULT_CFG), help="每群参数配置文件")
    p.add_argument("--status", action="store_true", help="只体检：端口/登录/每群下次窗口")
    p.add_argument("--dry-run", action="store_true", help="只显示参数与时间窗口，不调写接口")
    p.add_argument("--launch", action="store_true",
                   help="40653 不通时自动拉起 launcher-user.bat 并快速登录（免扫码）")
    p.add_argument("--no-import", action="store_true", help="只导出，不跑 archive.py import")
    p.add_argument("--only", help="只处理名字里含该子串的群")
    p.add_argument("--overlap", type=float, default=None,
                   help="从上次结束时间往前多导几分钟（默认读配置 overlap_minutes）")
    p.add_argument("--full", action="store_true", help="档案不存在时允许全量导出")
    args = p.parse_args(argv)

    cfg = load_cfg(Path(args.config))
    if args.overlap is None:
        args.overlap = float(cfg["overlap_minutes"])
    if args.status:
        return cmd_status(cfg, args.overlap)
    return cmd_run(cfg, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
