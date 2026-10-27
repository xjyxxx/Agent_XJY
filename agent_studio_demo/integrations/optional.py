"""可选第三方库的加载层：探测、版本、缺依赖时的清晰报错。

原则：**装了就多一种能力，不装一切照旧**。所有集成都必须能降级，
不能让"没装 mem0"变成"服务起不来"。
"""

from __future__ import annotations

import importlib
from typing import Optional


class DependencyMissing(RuntimeError):
    """没装可选依赖。消息里必须带安装命令。"""


def module_available(name: str) -> bool:
    """真的 import 一次，而不是 `find_spec`。

    `find_spec` 只查 finder：依赖被打桩、或包内部 import 失败时它仍返回 True，
    会让调用方走进"只有运行到一半才炸"的分支（这个坑在引擎选择上踩过）。
    """

    try:
        importlib.import_module(name)
    except Exception:  # noqa: BLE001 - 任何导入失败都视为不可用
        return False
    return True


def module_version(name: str) -> Optional[str]:
    if not module_available(name):
        return None
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001
        module = importlib.import_module(name)
        return getattr(module, "__version__", "installed")


def require(name: str, install: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        raise DependencyMissing(
            f"缺少可选依赖 {name}。安装：{install}\n"
            "（不装也能用：本项目有内置实现，见 docs/dependencies.md）"
        ) from exc


def status() -> dict:
    """给 `python -m integrations.status` 打印用。"""

    return {
        "mem0": {"available": module_available("mem0"), "version": module_version("mem0")},
        "ragas": {"available": module_available("ragas"), "version": module_version("ragas")},
        "opentelemetry": {
            "available": module_available("opentelemetry"),
            "version": module_version("opentelemetry"),
        },
    }


__all__ = [
    "DependencyMissing",
    "module_available",
    "module_version",
    "require",
    "status",
]
