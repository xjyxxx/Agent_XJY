"""可选集成（mem0 / RAGAS / OTel）。不装任何东西也能跑。"""

from integrations.optional import DependencyMissing, module_available, module_version, status

__all__ = ["DependencyMissing", "module_available", "module_version", "status"]
