"""社交平台账户凭证管理。"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

# ── .env 工具 ───────────────────────────────────────


def _read_env_value(key: str) -> str:
    """从 .env 文件中读取指定键的值。"""
    if not ENV_PATH.exists():
        return ""
    text = ENV_PATH.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(key)}=(.*)", re.MULTILINE)
    return m.group(1).strip() if m else ""


def _update_env_file(updates: dict[str, str]) -> bool:
    """更新 .env 文件中的指定键值对。"""
    if not ENV_PATH.exists():
        return False

    text = ENV_PATH.read_text(encoding="utf-8")
    changed = False
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text)
        else:
            text += f"\n{key}={value}\n"
        changed = True

    if changed:
        ENV_PATH.write_text(text, encoding="utf-8")
    return changed


def _set_env_vars(updates: dict[str, str]) -> None:
    """设置当前进程的环境变量（立即生效）。"""
    for key, value in updates.items():
        os.environ[key.upper()] = value


def _mask_value(value: str, visible: int = 4) -> str:
    """对敏感值做掩码显示，只保留前后各 visible 个字符。"""
    v = value.strip()
    if not v:
        return ""
    if len(v) <= visible * 2:
        return v[:visible] + "…" + v[-visible:] if len(v) > visible else v
    return v[:visible] + "…" + v[-visible:]


# ── B站 ─────────────────────────────────────────────

BILI_FIELDS = [
    ("bili_cookie_sessdata", "SESSDATA"),
    ("bili_cookie_bili_jct", "bili_jct"),
    ("bili_cookie_dedeuserid", "DedeUserID"),
]


def get_bilibili_status() -> dict:
    """获取 B站 Cookie 的当前状态。"""
    from backend.config.settings import Settings

    s = Settings()

    fields_info = []
    has_any = False
    for field_key, display_name in BILI_FIELDS:
        raw = getattr(s, field_key, "")
        is_set = bool(raw)
        if is_set:
            has_any = True
        fields_info.append({
            "key": field_key,
            "display": display_name,
            "set": is_set,
            "masked": _mask_value(raw) if is_set else "",
        })

    return {
        "platform": "bilibili",
        "configured": has_any,
        "summary": "已配置" if has_any else "未配置",
        "fields": fields_info,
    }


def test_bilibili_cookie() -> dict:
    """测试当前 Cookie 是否有效。"""
    from backend.services.bilibili.favorites import get_current_user

    try:
        user = get_current_user()
        return {
            "ok": True,
            "uname": user.get("uname", "?"),
            "mid": user.get("mid", 0),
        }
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"测试失败: {e}"}


def update_bilibili_fields(fields: dict[str, str]) -> dict:
    """更新 B站 Cookie 字段。

    Args:
        fields: {"bili_cookie_sessdata": "xxx", "bili_cookie_bili_jct": "xxx", ...}

    Returns:
        操作结果，包含测试结果。
    """
    env_updates = {}
    for field_key, _ in BILI_FIELDS:
        if field_key in fields:
            env_key = field_key.upper()
            env_updates[env_key] = fields[field_key]

    if not env_updates:
        return {"ok": False, "error": "没有要更新的字段"}

    # 写入 .env
    _update_env_file(env_updates)
    # 设置环境变量（立即生效）
    _set_env_vars(env_updates)

    # 测试
    test_result = test_bilibili_cookie()

    return {
        "ok": True,
        "updated": list(env_updates.keys()),
        "test": test_result,
    }


def clear_bilibili_cookie() -> dict:
    """清除 B站 Cookie。"""
    env_updates = {field_key.upper(): "" for field_key, _ in BILI_FIELDS}
    _update_env_file(env_updates)
    _set_env_vars(env_updates)
    return {"ok": True, "message": "B站 Cookie 已清除"}
