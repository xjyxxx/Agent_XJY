"""供外部服务器导入的 ASGI 工厂（granian / `uvicorn --factory`）。

独立进程里没有父进程的对象，所以这里**从环境变量重建** Service 与 EventBus：

    CHECKPOINT_PATH   SQLite 路径
    PROVIDER          auto / heuristic / deepseek
    UDP_PORT          >0 时把节点事件发到这个 UDP 端口（0=关闭）
    UDP_HOST          默认 127.0.0.1
    TRACE_JSONL       可选，同时落本地 JSONL

这样 granian 子进程也能带着观测通道跑，和父进程里的 Transport 行为一致。
"""

import os
from typing import Optional

from api.service import SessionService
from llm_client.factory import create_reply_model, load_dotenv
from transport.events import EventBus, JsonlSink, UdpSink
from transport.handlers import ApiHandlers
from transport.http.fastapi_adapter import create_app as build_fastapi_app


def _udp_port() -> Optional[int]:
    raw = os.environ.get("UDP_PORT", "0").strip()
    try:
        port = int(raw or 0)
    except ValueError:
        return None
    return port or None


def build_bus() -> EventBus:
    sinks = []
    port = _udp_port()
    if port:
        sinks.append(UdpSink(os.environ.get("UDP_HOST", "127.0.0.1"), port))
    jsonl = os.environ.get("TRACE_JSONL", "").strip()
    if jsonl:
        sinks.append(JsonlSink(jsonl))
    return EventBus(sinks)


def build_service() -> SessionService:
    load_dotenv()
    return SessionService(
        os.environ.get("CHECKPOINT_PATH", "runtime/api.sqlite3"),
        model=create_reply_model(os.environ.get("PROVIDER", "auto")),
    )


def create_app():
    """ASGI factory: granian `--factory`, uvicorn `--factory`."""

    return build_fastapi_app(service=build_service(), bus=build_bus())


__all__ = ["build_bus", "build_service", "create_app", "ApiHandlers"]
