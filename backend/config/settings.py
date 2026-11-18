"""统一配置 — 通过 pydantic-settings 从环境变量读取。"""

import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.path_setup import PROJECT_ROOT

# ── 路径引导 ──
from backend.path_setup import ensure as _ensure_path
_ensure_path()
# ──────────────

from backend.config.deployment import require_env

from backend.core.llm.client import LLMClient, LLMConfig


def _project_root() -> Path:
    _p = Path(__file__).resolve()
    for parent in _p.parents:
        if (parent / "CLAUDE.md").exists() or (parent / "README.md").exists():
            return parent
    return _p.parent.parent.parent  # fallback


def _resolve_api_key(value: str) -> str:
    if isinstance(value, str) and value.startswith("ENV:"):
        key, = require_env(value[4:].strip())
        return key
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 项目路径 ──────────────────────────────────
    project_root: Path = Path(_project_root())
    output_root: Path = Path("./output")
    data_dir: Path = Path("./output/data")
    job_storage_dir: Path = Path("./output/data/jobs")

    # ── LLM ───────────────────────────────────────
    llm_config_path: Path = Path(__file__).resolve().parent / "model_config.json"
    deepseek_api_key: str = ""
    glm_api_key: str = ""

    # ── SSH (remote PDF) ──────────────────────────
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_key_path: Path | None = None
    ssh_password: str = ""
    ssh_timeout: int = 10

    # ── SSH 代理配置 ──────────────────────────────
    ssh_proxy_host: str = ""
    ssh_proxy_port: int = 0
    ssh_proxy_type: str = "socks5"

    # ── PDF 远程路径 ──────────────────────────────
    remote_python: str = ""
    remote_activate: str = ""
    remote_script_path: str = ""
    remote_target_dir: str = ""
    remote_input_dir: str = ""
    remote_output_dir: str = ""
    remote_log_dir: str = ""
    remote_convert_script: str = ""

    # ── 本地 PDF 路径 ────────────────────────────
    local_pdfs_dir: Path = Path("./data/pdfs")
    local_test_file: Path = Path("./output/test_input.txt")
    local_output_file: Path = Path("./output/downloaded_output.txt")

    # ── PDF 远程翻译路径 ───────────────────────────
    remote_translate_script: str = ""
    remote_translate_dir: str = ""

    # ── PDF 远程 MinerU 路径 ───────────────────────
    remote_mineru_script: str = ""
    remote_mineru_input_dir: str = ""
    remote_mineru_output_dir: str = ""
    remote_mineru_log_dir: str = ""

    # ── 远端 GPU / Ollama 预检 ─────────────────────
    # 提交 marker/mineru 前：先跑远端 ollama_cleanup.py（只清理 UNTIL=Forever
    # 且最近无请求的模型），再检查 GPU 空闲显存；低于阈值直接报错不上传。
    remote_ollama_cleanup_script: str = ""
    remote_min_gpu_free_mb: int = 6000

    # ── 脚本执行 ──────────────────────────────────
    script_wait_time: int = 12

    # ── Bilibili ──────────────────────────────────
    bili_cookie: str = ""
    bili_cookie_sessdata: str = ""
    bili_cookie_bili_jct: str = ""
    bili_cookie_dedeuserid: str = ""

    # ── Community / QQ ───────────────────────────
    # QQ 导出/归档仍依赖 D:\QQChatExporter 的 NapCat+QCE 独立服务；
    # infosystem 只做正式 CLI/API 封装和调度，不复制它的登录态。
    qq_pipeline_code_root: Path = PROJECT_ROOT / "backend" / "services" / "community" / "qq" / "pipeline"
    qq_pipeline_data_root: Path = PROJECT_ROOT / "data" / "community" / "qq" / "pipeline"
    qq_export_config: Path = PROJECT_ROOT / "backend" / "services" / "community" / "qq" / "pipeline" / "export_groups.json"
    qq_export_timeout_seconds: int = 3600
    qq_readonly_timeout_seconds: int = 180

    # ── Remote FS（远程文件系统）───────────────────
    remote_fs_http_url: str = ""
    remote_fs_ftp_host: str = ""
    remote_fs_ftp_port: int = 21
    remote_fs_ftp_user: str = ""
    remote_fs_ftp_pass: str = ""

    # ── 批处理 ────────────────────────────────────
    default_workers: int = 4

    # ─────────────────────────────────────────────

    def get_llm_config(self, model_key: str) -> LLMConfig:
        path = Path(self.llm_config_path)
        if not path.exists():
            raise FileNotFoundError(
                f"LLM 配置缺失：{path}\n"
                "  1) 复制模板：Copy-Item backend\\config\\model_config.example.json backend\\config\\model_config.json\n"
                "  2) 在项目根 .envs 里填 GLM_API_KEY / DEEPSEEK_API_KEY 等（模板里的 api_key 是 ENV: 引用）\n"
                "  3) 模型 key 可选：glm-flash / v4flash / deepseek-chat / glm-4.6v_ocr ..."
            )

        configs = json.loads(path.read_text(encoding="utf-8"))
        if model_key not in configs:
            available = list(configs.keys())
            raise KeyError(f"Model '{model_key}' not found. Available: {available}")

        raw = configs[model_key]
        return LLMConfig(
            endpoint=raw["endpoint"],
            api_key=_resolve_api_key(raw.get("api_key", "")),
            model=raw.get("name", model_key),
            max_retries=raw.get("max_retries", 3),
            retry_base_delay=raw.get("retry_base_delay", 2.0),
            request_template=raw.get("request_template"),
            job_api_type=raw.get("job_api_type", "openai"),
            timeout=int(raw.get("timeout", 120)),
        )

    def get_llm_client(self, model_key: str) -> LLMClient:
        return LLMClient(self.get_llm_config(model_key))
