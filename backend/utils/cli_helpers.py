"""CLI 辅助工具 — 文本参数文件解析、配置文件合并等。

用法:
    from backend.utils.cli_helpers import resolve_text, apply_config_file

    # --foo / --foo-file 二选一
    text = resolve_text(args.foo, args.foo_file)

    # --config 配置文件合并
    cfg = apply_config_file(args, "persona")
"""

import json
from pathlib import Path
from typing import Any


# ── 各子命令的 CLI 默认值表 ──────────────────────
# 用于判断用户是否显式提供了某参数（vs 使用了默认值）。
# key = subcommand, value = {arg_dest: default_value}
_SUBCOMMAND_DEFAULTS: dict[str, dict[str, Any]] = {
    "persona": {
        "input": "",
        "output": "",
        "model": "v4flash",
        "user_pattern": r"^##\s*【您】",
        "ai_pattern": r"^##\s*【对方】",
        "story": "",
        "story_file": "",
        "task_prompt": "",
        "task_prompt_file": "",
        "workers": 4,
        "force": False,
        "config": "",
    },
    # 后续子命令可在此追加
}


def resolve_text(value: str | None, file_path: str | None = None) -> str:
    """解析长文本参数的两种提供方式：直接值 或 读取文件。

    Args:
        value:     CLI 参数值（如 --foo 的值，可为 "" 或 None）。
        file_path: 文件路径（如 --foo-file 的值）。

    Returns:
        若 file_path 存在则读取文件内容；否则返回原 value（若为 None 则返回 ""）。
    """
    if file_path:
        p = Path(file_path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return value or ""


def load_json_config(config_path: str) -> dict[str, Any]:
    """加载 JSON 配置文件。"""
    return json.loads(Path(config_path).read_text(encoding="utf-8"))


def apply_config_file(args, subcommand: str) -> None:
    """将 --config 文件中的参数合并到 args 中（CLI 显式值优先）。

    对于 args 中仍为默认值的参数，若 config 文件提供了同名的 key，则覆盖。
    该函数直接修改 args 对象。

    Args:
        args:       已由 argparse 完成解析的 Namespace。
        subcommand: 子命令名，用于查找对应的默认值表。
    """
    config_path: str | None = getattr(args, "config", None)
    if not config_path:
        return

    defaults = _SUBCOMMAND_DEFAULTS.get(subcommand)
    if defaults is None:
        raise KeyError(f"未知子命令，未注册默认值表: {subcommand}")

    config = load_json_config(config_path)

    for key, cfg_value in config.items():
        if not hasattr(args, key):
            continue  # config 里有但 CLI 没有的 key 忽略
        current = getattr(args, key)
        default = defaults.get(key)

        # 仅当 CLI 值仍为默认时，才用 config 覆盖
        if current == default:
            setattr(args, key, cfg_value)
