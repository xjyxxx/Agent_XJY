"""兼容入口（保留 `api.main` 这个路径）。

真正的实现在 `transport/` 包里：
- 业务逻辑：`transport.handlers.ApiHandlers`（不依赖任何 HTTP 框架）
- 协议适配：`transport.http.fastapi_adapter`（可选依赖）/
            `transport.http.stdlib_adapter`（零依赖）
- 组合与生命周期：`transport.bundle.Transport`（TCP 服务 + UDP 上报）

这里只做转发，保证老的调用方式仍然有效：
    from api.main import create_app
    python -m api.main
"""

from __future__ import annotations

import os

from transport.http.fastapi_adapter import DependencyMissing, create_app  # noqa: F401

__all__ = ["DependencyMissing", "create_app", "main"]


def main() -> int:
    from transport.bundle import serve_forever

    serve_forever(
        host=os.environ.get("HOST", "127.0.0.1"),
        tcp_port=int(os.environ.get("PORT", "8000")),
        udp_port=int(os.environ.get("UDP_PORT", "0")) or None,
        jsonl_path=os.environ.get("TRACE_JSONL"),
        adapter=os.environ.get("ADAPTER", "auto"),
        server=os.environ.get("SERVER", "auto"),
        checkpoint_path=os.environ.get("CHECKPOINT_PATH", "runtime/api.sqlite3"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
