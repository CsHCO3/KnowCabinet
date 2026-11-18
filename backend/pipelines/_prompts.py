"""提示词加载 — 从 prompts 目录读取模板文件。"""

import re
from pathlib import Path


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(template_name: str, sub_dir: str | None = None, **kwargs: str) -> tuple[str, str]:
    """加载提示词模板并进行变量替换。

    模板文件位置: {PROMPTS_DIR}/{sub_dir}/{template_name}_{system,user}.txt
    占位符语法: {{var_name}} 或 $var_name

    Returns:
        (system_prompt, user_prompt)
    """
    base_dir = _PROMPTS_DIR
    if sub_dir:
        base_dir = base_dir / sub_dir

    system_path = base_dir / f"{template_name}_system.txt"
    user_path = base_dir / f"{template_name}_user.txt"

    if not system_path.exists():
        raise FileNotFoundError(f"系统提示词模板不存在: {system_path}")
    if not user_path.exists():
        raise FileNotFoundError(f"用户提示词模板不存在: {user_path}")

    system_prompt = system_path.read_text(encoding="utf-8")
    user_prompt = user_path.read_text(encoding="utf-8")

    if kwargs:
        system_prompt = _render(system_prompt, kwargs)
        user_prompt = _render(user_prompt, kwargs)

    return system_prompt, user_prompt


def load_system_prompt(template_name: str, sub_dir: str | None = None, **kwargs: str) -> str:
    """加载仅系统提示词（不要求 _user.txt 存在）。

    用于单提示词任务（如 OCR）：{PROMPTS_DIR}/{sub_dir}/{template_name}_system.txt。
    """
    base_dir = _PROMPTS_DIR
    if sub_dir:
        base_dir = base_dir / sub_dir
    path = base_dir / f"{template_name}_system.txt"
    if not path.exists():
        raise FileNotFoundError(f"系统提示词模板不存在: {path}")
    text = path.read_text(encoding="utf-8")
    if kwargs:
        text = _render(text, kwargs)
    return text


def _render(template: str, variables: dict) -> str:
    """替换模板中的 {{var}} 和 $var 占位符。"""
    def _replace(m: re.Match) -> str:
        return str(variables.get(m.group(1), m.group(0)))
    rendered = re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", _replace, template)
    rendered = re.sub(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", _replace, rendered)
    return rendered
