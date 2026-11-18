"""服务容器 — 懒加载、可替换、可用于 mock。"""

from typing import Any


class ServiceContainer:
    """基于工厂的轻量 DI 容器。

    用法:
        container = ServiceContainer()
        container.register("arxiv", lambda: ArxivService())
        svc = container.get("arxiv")
    """

    def __init__(self):
        self._factories: dict[str, callable] = {}
        self._instances: dict[str, Any] = {}

    def register(self, name: str, factory: callable) -> None:
        self._factories[name] = factory

    def register_instance(self, name: str, instance: Any) -> None:
        self._instances[name] = instance

    def get(self, name: str) -> Any:
        if name in self._instances:
            return self._instances[name]
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(f"Service not registered: {name}")
        svc = factory()
        self._instances[name] = svc
        return svc

    def has(self, name: str) -> bool:
        return name in self._instances or name in self._factories

    def override(self, name: str, mock: Any) -> None:
        """注入 mock 用于测试。"""
        self._instances[name] = mock
