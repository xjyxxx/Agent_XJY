"""压测用的服务器进程入口：`python -m bench.serve --server granian --port 8123`。

为什么要单独一个进程：如果服务器和压测脚本同进程，GIL 会让"服务器"看起来慢一个
数量级，测出来的不是框架性能而是 Python 的线程调度。所有后端必须在独立进程里跑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from transport.bundle import serve_forever


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark target server")
    parser.add_argument("--adapter", choices=("fastapi", "stdlib"), required=True)
    parser.add_argument(
        "--server", choices=("uvicorn", "granian", "stdlib"), required=True
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    serve_forever(
        host=args.host,
        tcp_port=args.port,
        adapter=args.adapter,
        server=args.server,
        checkpoint_path=":memory:",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
