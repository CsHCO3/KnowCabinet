"""贴吧正式 service：抓取、列表和知识点提炼。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, parse_qs

from backend.path_setup import PROJECT_ROOT
from backend.services.community.tieba.legacy_client import TiebaClient
from backend.services.community.tieba.paths import (
    COOKIE_FILE,
    KNOWLEDGE_DIR,
    SOURCE_LIST_DIR,
    ensure_dirs,
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha16(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _unwrap_links(text: str) -> str:
    """贴吧 checkurl 跳转还原为直链，便于抽取项目资源。"""
    def repl(match: re.Match) -> str:
        try:
            query = parse_qs(match.group(1))
            return unquote(query.get("url", [match.group(0)])[0])
        except Exception:
            return match.group(0)
    return re.sub(r"tieba\.baidu\.com/mo/q/checkurl\?url=([^&\s]+)", repl, text)


class TiebaService:
    """正式贴吧入口。默认不保存全帖历史，只保存知识点和被引用证据。"""

    def __init__(self, knowledge_dir: Path | None = None):
        ensure_dirs()
        self.client = TiebaClient()
        self.client.load_cookies()
        self.knowledge_dir = Path(knowledge_dir or KNOWLEDGE_DIR)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    # ── 抓取 ───────────────────────────────────────────────
    def thread(self, tid: str | int, page: int | None = None) -> dict:
        return self.client.read_thread(tid, page=page)

    def forum(self, forum_name: str, page: int = 1) -> dict:
        return self.client.read_forum(forum_name, page)

    def featured(self, forum_name: str, page: int = 1) -> dict:
        return self.client.read_featured(forum_name, page)

    def search(self, forum_name: str, keyword: str, page: int = 1, max_pages: int = 1) -> dict:
        return self.client.search(forum_name, keyword, page=page, max_pages=max_pages)

    def favorites(self, max_pages: int = 0) -> dict:
        from backend.services.community.tieba.legacy_client import read_account_favorites
        return read_account_favorites(max_pages=max_pages)

    def save_source_list(self, name: str, content: str) -> Path:
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(". ") or "list"
        path = SOURCE_LIST_DIR / f"{safe}.txt"
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    # ── 知识点 ─────────────────────────────────────────────
    def knowledge_path(self, tid: str | int) -> Path:
        return self.knowledge_dir / f"thread_{tid}.json"

    def load_knowledge(self, tid: str | int) -> dict | None:
        path = self.knowledge_path(tid)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def extract(
        self,
        tid: str | int,
        *,
        llm,
        update: bool = True,
        max_chars: int = 15000,
        model_key: str = "deepseek-chat",
    ) -> dict:
        """抓全帖、提炼知识点，但只持久化知识点和被引用证据。"""
        from backend.pipelines._prompts import load_system_prompt
        from backend.services.community.tieba.knowledge import TiebaKnowledgeExtractor

        doc = self.thread(tid)
        extractor = TiebaKnowledgeExtractor(llm=llm, knowledge_dir=self.knowledge_dir)
        return extractor.extract(
            doc,
            llm=llm,
            update=update,
            max_chars=max_chars,
            load_prompt=load_system_prompt,
        )
