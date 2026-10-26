"""静态资源托管（前端页面）。

两个约束决定了实现方式：
1. **不能引入 Node/Vite**：这个项目的前端是单页 + 原生 JS，不需要构建步骤；
   一旦引入 npm，`adapter=stdlib` 的"零依赖"承诺就破了。
2. **必须同时被两个适配器复用**：所以路径解析、内容类型、越界防护都写在这里，
   适配器只负责把 bytes 写出去。

安全：只允许读 `web/` 目录下的文件，且只允许白名单后缀。
任何 `..`、绝对路径、符号链接逃逸都会被拒绝——这是"导入 path 任意读"那类漏洞的
同一类问题，所以在这里一次做对。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

WEB_ROOT = (Path(__file__).resolve().parents[2] / "web").resolve()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}

MAX_BYTES = 2 * 1024 * 1024


class StaticError(Exception):
    """Raised when a path escapes the web root or is not allowed."""


def web_root() -> Path:
    return WEB_ROOT


def resolve(path: str) -> Path:
    """Map a URL path to a file inside `web/`, or raise StaticError."""

    cleaned = (path or "/").split("?", 1)[0].split("#", 1)[0]
    if cleaned in ("", "/"):
        relative = "index.html"
    else:
        relative = cleaned.lstrip("/")

    candidate = (WEB_ROOT / relative).resolve()
    try:
        candidate.relative_to(WEB_ROOT)
    except ValueError as exc:
        raise StaticError(f"path escapes web root: {path!r}") from exc

    if candidate.suffix.lower() not in CONTENT_TYPES:
        raise StaticError(f"unsupported static type: {candidate.suffix!r}")
    return candidate


def load(path: str) -> Tuple[str, bytes]:
    """Return (content_type, body) for a URL path."""

    file_path = resolve(path)
    if not file_path.is_file():
        raise StaticError(f"not found: {path!r}")
    if file_path.stat().st_size > MAX_BYTES:
        raise StaticError(f"file too large: {path!r}")
    return CONTENT_TYPES[file_path.suffix.lower()], file_path.read_bytes()


def try_load(path: str) -> Optional[Tuple[str, bytes]]:
    """Same as load(), but returns None instead of raising."""

    try:
        return load(path)
    except (StaticError, OSError):
        return None
