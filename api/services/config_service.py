import json
from urllib.parse import urlparse
from pathlib import Path
from api.schemas.common import ModelInfo, PromptDir, PromptFile, PromptContent


BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
CONFIG_JSON = BACKEND_DIR / "config" / "model_config.json"
PROMPTS_DIR = BACKEND_DIR / "pipelines" / "prompts"


class ConfigService:

    def list_models(self) -> list[ModelInfo]:
        if not CONFIG_JSON.exists():
            return []
        with open(CONFIG_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        models = []
        for key, cfg in raw.items():
            endpoint = cfg.get("endpoint", "")
            display_name = cfg.get("name") or cfg.get("display_name", key)
            # 从 endpoint 提取 provider 名
            parsed = urlparse(endpoint)
            provider = parsed.hostname or "unknown"
            # 检测连通性
            status = self._check_endpoint(endpoint)
            models.append(ModelInfo(
                key=key,
                display_name=display_name,
                provider=provider,
                endpoint=endpoint,
                status=status,
            ))
        return models

    def _check_endpoint(self, url: str) -> str:
        """HTTP 检测 API 端点是否可达（不消耗额度，只检查响应）"""
        if not url:
            return "unavailable"
        try:
            from urllib.request import Request, urlopen
            from urllib.error import URLError
            req = Request(url, method="GET")
            # 设置短超时，不发送 body
            try:
                resp = urlopen(req, timeout=4)
                resp.read()
                resp.close()
            except URLError as e:
                # 4xx/5xx 也算可达（服务器在运行，只是路径/权限问题）
                if hasattr(e, 'code') and e.code is not None:
                    return "available"
                return "unavailable"
            return "available"
        except Exception:
            return "unavailable"

    def get_model(self, key: str) -> ModelInfo | None:
        for m in self.list_models():
            if m.key == key:
                return m
        return None

    def get_prompt_tree(self) -> PromptDir:
        tree = self._build_tree(PROMPTS_DIR, "prompts")
        self._attach_usage(tree)
        return tree

    def _load_usage_map(self) -> dict[str, str]:
        meta_path = PROMPTS_DIR / "prompts_meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, Exception):
                return {}
        return {}

    def _attach_usage(self, node: PromptDir) -> None:
        """递归附加用途说明到每个文件节点。"""
        usage_map = self._load_usage_map()
        self._apply_usage(node, "prompts", usage_map)

    def _apply_usage(self, node: PromptDir, base_path: str, usage_map: dict[str, str]) -> None:
        for file in node.files:
            rel = f"{base_path}/{file.filename}"[len("prompts/"):]  # 取相对路径
            file.usage = usage_map.get(rel, "")
        for sub in node.subdirs:
            self._apply_usage(sub, f"{base_path}/{sub.dirname}", usage_map)

    def _build_tree(self, directory: Path, rel_path: str) -> PromptDir:
        node = PromptDir(path=rel_path, dirname=directory.name)
        if not directory.exists():
            return node
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                node.subdirs.append(self._build_tree(child, f"{rel_path}/{child.name}"))
            elif child.suffix in (".txt", ".md", ".json", ".yaml", ".toml") and child.name != "prompts_meta.json":
                node.files.append(PromptFile(path=f"{rel_path}/{child.name}", filename=child.name))
        return node

    def get_prompt_content(self, path: str) -> PromptContent | None:
        target = PROMPTS_DIR / path.removeprefix("prompts/")
        if not target.exists() or not target.is_file():
            return None
        with open(target, encoding="utf-8") as f:
            content = f.read()
        return PromptContent(path=path, filename=target.name, content=content)
