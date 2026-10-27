"""本地配置加载（INI 格式，零依赖）。

为什么用 INI 而不是 `.env`：
1. 支持注释与分组，模板本身就是"配置说明"，别人 clone 下来知道要填什么；
2. 一个文件管所有设置（模型 / 服务 / 功能开关 / 检索），不用记一堆环境变量名；
3. 标准库 `configparser` 就够，不引入 pyyaml / tomli（Python 3.10 没有 tomllib）。

**密钥永远只在本地**：
- 模板 `config/settings.ini.example` 提交到仓库，值全是空的；
- 真实配置 `config/settings.ini` 在 `.gitignore` 里，永远不会被提交。

优先级（高 → 低）：
    进程环境变量  >  config/settings.ini  >  .env（兼容旧方式）  >  代码默认值

只"补齐"不"覆盖"：这样 `$env:DEEPSEEK_API_KEY=...` 仍然能临时压过配置文件。
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.ini"
EXAMPLE_PATH = ROOT / "config" / "settings.ini.example"
LEGACY_ENV_PATH = ROOT / ".env"

# (section, key) -> 环境变量名。新增配置项只改这一张表。
ENV_MAP: Dict[tuple, str] = {
    ("model", "api_key"): "DEEPSEEK_API_KEY",
    ("model", "base_url"): "DEEPSEEK_BASE_URL",
    ("model", "model"): "DEEPSEEK_MODEL",
    ("model", "max_retries"): "LLM_MAX_RETRIES",
    ("model", "retry_base_delay"): "LLM_RETRY_BASE_DELAY",
    ("model", "embedding_base_url"): "EMBEDDING_BASE_URL",
    ("model", "embedding_api_key"): "EMBEDDING_API_KEY",
    ("model", "embedding_model"): "EMBEDDING_MODEL",
    ("server", "provider"): "PROVIDER",
    ("server", "graph_engine"): "GRAPH_ENGINE",
    ("server", "adapter"): "ADAPTER",
    ("server", "server_backend"): "SERVER",
    ("server", "host"): "HOST",
    ("server", "port"): "PORT",
    ("server", "udp_port"): "UDP_PORT",
    ("server", "checkpoint_path"): "CHECKPOINT_PATH",
    ("server", "trace_jsonl"): "TRACE_JSONL",
    ("server", "session_ttl"): "SESSION_TTL",
    ("server", "session_max"): "SESSION_MAX",
    ("features", "stream"): "STREAM",
    ("features", "persona_llm"): "PERSONA_LLM",
    ("features", "expand_query"): "EXPAND_QUERY",
    ("features", "chat_root"): "CHAT_ROOT",
    ("search", "min_evidence_score"): "MIN_EVIDENCE_SCORE",
}

# 这些是敏感项：打印摘要时只说"有没有配"，绝不输出值。
SECRET_KEYS = {"DEEPSEEK_API_KEY", "EMBEDDING_API_KEY"}

# 记录每个键"从哪来"。bootstrap 会填；describe 用它，避免把 ini 的值误报成环境变量
# （bootstrap 会把值写进 os.environ，事后无法分辨来源）。
_ORIGIN: Dict[str, str] = {}


def read_settings(path: Optional[Path] = None) -> Dict[str, str]:
    """读取 settings.ini，返回 {环境变量名: 值}；文件不存在则返回空。"""

    settings_path = Path(path or SETTINGS_PATH)
    if not settings_path.is_file():
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    # 保留大小写（key 都是小写，但值可能是路径/URL）
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(settings_path, encoding="utf-8")

    values: Dict[str, str] = {}
    for (section, key), env_name in ENV_MAP.items():
        if not parser.has_option(section, key):
            continue
        value = parser.get(section, key).strip()
        if value:
            values[env_name] = value
    return values


def read_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """兼容旧的 .env（KEY=VALUE）；存在才读。"""

    env_path = Path(path or LEGACY_ENV_PATH)
    if not env_path.is_file():
        return {}
    values: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value:
            values[key] = value
    return values


def apply(values: Dict[str, str]) -> list[str]:
    """把配置补进 os.environ（不覆盖已存在的），返回实际写入的键名。"""

    applied: list[str] = []
    for key, value in values.items():
        if key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def bootstrap(path: Optional[Path] = None) -> dict:
    """加载本地配置。可重复调用（第二次不会产生变化）。"""

    settings = read_settings(path)
    settings_name = str(path or SETTINGS_PATH)
    from_settings = apply(settings)
    for key in from_settings:
        _ORIGIN[key] = settings_name
    from_env_file = apply(read_env_file())
    for key in from_env_file:
        _ORIGIN.setdefault(key, str(LEGACY_ENV_PATH))
    return {
        "settings_file": settings_name,
        "settings_exists": Path(path or SETTINGS_PATH).is_file(),
        "applied_from_settings": from_settings,
        "applied_from_dotenv": from_env_file,
    }


def describe() -> dict:
    """可安全打印的配置摘要：不含任何密钥值。"""

    settings = read_settings()
    env_file = read_env_file()
    # 先看 bootstrap 记录的来源；没有记录说明是进程启动前就存在的环境变量
    configured_by = _ORIGIN.get("DEEPSEEK_API_KEY")
    if configured_by is None:
        if os.environ.get("DEEPSEEK_API_KEY"):
            configured_by = "environment"
        elif "DEEPSEEK_API_KEY" in settings:
            configured_by = "config/settings.ini"
        elif "DEEPSEEK_API_KEY" in env_file:
            configured_by = ".env"
    return {
        "settings_file": str(SETTINGS_PATH),
        "settings_exists": SETTINGS_PATH.is_file(),
        "example_exists": EXAMPLE_PATH.is_file(),
        "key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "key_configured_by": configured_by,
        "model": os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat",
        "base_url": os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        "secret_keys_present": sorted(k for k in SECRET_KEYS if os.environ.get(k)),
    }


def status_line() -> str:
    """一行状态，给 manage.py / 预检打印用。"""

    info = describe()
    source = info["key_configured_by"] or ""
    if source.endswith("settings.ini"):
        source = "config/settings.ini"
    elif source.endswith(".env"):
        source = ".env"
    if info["key_configured"]:
        return f"模型密钥已配置（来源：{source}），model={info['model']}"
    return (
        "未配置模型密钥，将使用离线 heuristic。"
        f"本地配置：复制 {EXAMPLE_PATH.name} 为 {SETTINGS_PATH.name} 并填写"
    )


__all__ = [
    "ENV_MAP",
    "EXAMPLE_PATH",
    "LEGACY_ENV_PATH",
    "SECRET_KEYS",
    "SETTINGS_PATH",
    "apply",
    "bootstrap",
    "describe",
    "read_env_file",
    "read_settings",
    "status_line",
]
