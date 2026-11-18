"""万方 gRPC 搜索请求 A1 重放测试（纯 HTTP，不依赖浏览器/agent）。

请求体不在仓库里：用环境变量 WANFANG_REQ 指向本地抓到的 .bin，
默认读本文件同目录的 wanfang_search_request.bin。
"""
from __future__ import annotations
import json, os, urllib.request
from pathlib import Path

from backend.services.papers.session_paths import storage_state

SS = str(storage_state("wanfang"))  # data/paper_sessions/wanfang/storage_state.json（本地数据，不进仓库）
REQ = os.environ.get("WANFANG_REQ") or str(Path(__file__).resolve().parent / "wanfang_search_request.bin")
URL = "https://s.wanfangdata.com.cn/SearchService.SearchService/search"

# 从 storage state 取 CASTGC
state = json.load(open(SS, encoding="utf-8"))
castgc = next((c["value"] for c in state["cookies"] if c["name"] == "CASTGC"), "")
print("CASTGC 取到:", bool(castgc))

body = open(REQ, "rb").read()
print("请求体字节:", len(body))

headers = {
    "content-type": "application/grpc-web+proto",
    "x-grpc-web": "1",
    "x-user-agent": "grpc-web-javascript/0.1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "cookie": f"CASTGC={castgc};CASTGCSpecial=;",
    "referer": "https://s.wanfangdata.com.cn/paper?q=%E5%B7%A5%E4%B8%9A%E4%BA%92%E8%81%94%E7%BD%91%E6%A0%87%E8%AF%86%E8%A7%A3%E6%9E%90&p=1",
    "origin": "https://s.wanfangdata.com.cn",
}

req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        rb = resp.read()
        print("HTTP:", resp.status)
        print("响应字节:", len(rb))
        print("content-type:", resp.headers.get("content-type"))
        auth = resp.headers.get("auth-context", "")
        print("auth-context 前120:", auth[:120])
        open(r"C:\Users\HP\Desktop\论文搜索\wanfang_search_response.bin", "wb").write(rb)
        print("响应已存: wanfang_search_response.bin")
except Exception as e:
    print("失败:", type(e).__name__, e)
