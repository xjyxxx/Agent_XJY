"""本地配置（INI，零依赖）。真实配置文件 settings.ini 不入库。"""

from config.loader import (
    bootstrap,
    describe,
    read_env_file,
    read_settings,
    status_line,
)

__all__ = ["bootstrap", "describe", "read_env_file", "read_settings", "status_line"]
