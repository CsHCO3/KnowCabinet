"""Load private deployment settings without requiring optional services at startup."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".envs", interpolate=False)


def require_env(*names: str) -> tuple[str, ...]:
    values = tuple(os.environ.get(name, "").strip() for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError("请在 .envs 配置 " + ", ".join(missing))
    return values
