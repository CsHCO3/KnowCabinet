"""贴吧正式化路径。原始 JSON 不作为长期档案；只保存知识点和证据。"""

from __future__ import annotations

import os
from pathlib import Path

from backend.path_setup import PROJECT_ROOT


DATA_DIR = Path(os.environ.get("TIEBA_DATA_DIR", PROJECT_ROOT / "data" / "community" / "tieba"))
CONFIG_DIR = DATA_DIR / "config"
COOKIE_FILE = CONFIG_DIR / "cookies.json"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
SOURCE_LIST_DIR = DATA_DIR / "source_lists"
TEMP_DIR = DATA_DIR / "tmp"


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, KNOWLEDGE_DIR, SOURCE_LIST_DIR, TEMP_DIR):
        path.mkdir(parents=True, exist_ok=True)
