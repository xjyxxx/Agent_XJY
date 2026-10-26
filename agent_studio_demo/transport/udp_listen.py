"""UDP 观测事件的接收端（开发/演示用）。

    # 终端 A
    python -m transport.udp_listen --port 8125

    # 终端 B
    python -m transport --udp-port 8125

    # 终端 C：发一轮对话，终端 A 会打出每个节点的事件
    curl ... /sessions/<sid>/stream?message=...

打印格式刻意压成一行：观测事件要能一眼扫过去，不需要格式化得很漂亮。
"""

from __future__ import annotations

import argparse
import json
import socket
from datetime import datetime


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Listen for UDP telemetry events")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8125)
    parser.add_argument("--max-bytes", type=int, default=8192)
    args = parser.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    # flush=True：被重定向到文件/管道时 stdout 是块缓冲，不 flush 就看不到实时事件。
    print(f"listening udp://{args.host}:{args.port} ... (Ctrl+C to stop)", flush=True)
    try:
        while True:
            payload, address = sock.recvfrom(args.max_bytes)
            try:
                event = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                print(f"[{address[0]}:{address[1]}] <non-json {len(payload)}B>", flush=True)
                continue
            stamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
            print(
                f"{stamp.strftime('%H:%M:%S.%f')[:-3]} "
                f"{event.get('event', '?'):<14} "
                f"status={event.get('status')} "
                f"turn={event.get('turn')} "
                f"hits={event.get('hits')} "
                f"verdict={event.get('verdict')} "
                f"ms_prev={event.get('ms_since_prev')}",
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
