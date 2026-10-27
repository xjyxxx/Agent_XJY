"""统一入口：一个进程里同时提供 TCP 服务与 UDP 上报。

    python -m transport                          # auto 适配器，TCP 8000，不开 UDP
    python -m transport --udp-port 8125          # 同时往 127.0.0.1:8125 发观测事件
    python -m transport --adapter stdlib --port 0  # 零依赖适配器（端口 0 = 随机）
    python -m transport --jsonl runtime/trace.jsonl

配合接收端：
    python -m transport.udp_listen --port 8125
"""

from __future__ import annotations

import argparse
import os
import sys

from transport.bundle import serve_forever


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Persona Replay transport (TCP + UDP)")
    parser.add_argument(
        "--adapter",
        choices=("auto", "fastapi", "stdlib"),
        default="auto",
        help="auto: 有 fastapi+uvicorn 就用它，否则退回标准库实现",
    )
    parser.add_argument(
        "--server",
        choices=("auto", "uvicorn", "granian", "stdlib"),
        default=os.environ.get("SERVER", "auto"),
        help="auto: granian > uvicorn > stdlib（按实测吞吐排序，选已安装的）",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--udp-port",
        type=int,
        default=int(os.environ.get("UDP_PORT", "0")) or None,
        help="观测事件的 UDP 目标端口；0 表示关闭",
    )
    parser.add_argument(
        "--jsonl",
        default=os.environ.get("TRACE_JSONL"),
        help="同时把观测事件写到本地 JSONL（开发期用）",
    )
    parser.add_argument(
        "--checkpoint",
        default=os.environ.get("CHECKPOINT_PATH", "runtime/api.sqlite3"),
    )
    args = parser.parse_args(argv)

    from memory.checkpoint import CheckpointLocked, ensure_writable

    try:
        ensure_writable(args.checkpoint)
    except CheckpointLocked as exc:
        print(f"[xx] {exc}", file=sys.stderr)
        return 2

    serve_forever(
        host=args.host,
        tcp_port=args.port,
        udp_port=args.udp_port,
        jsonl_path=args.jsonl,
        adapter=args.adapter,
        server=args.server,
        checkpoint_path=args.checkpoint,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
