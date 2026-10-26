"""传输层：把"业务"和"协议"分开（Day14 重构）。

分层：

    transport.handlers   ← 纯业务：入参 dict、出参 (status, payload)、SSE 事件迭代器
    transport.http.*     ← 协议适配器：FastAPI（可选依赖）/ 标准库 http.server（零依赖）
    transport.events     ← 观测通道：EventBus → 多个 Sink（UDP / JSONL / Null）
    transport.bundle     ← 生命周期：一个进程里同时起 TCP 服务与 UDP 上报

关键边界：**HTTP 不能跑在 UDP 上**（HTTP/3 走 QUIC，需要专门库），SSE 也只能走 TCP。
所以 UDP 只承载"丢了也不影响正确性"的观测事件，且事件里不带任何对话内容。
"""

from transport.events import EventBus, JsonlSink, NullSink, UdpSink
from transport.handlers import ApiHandlers, ValidationError

__all__ = [
    "ApiHandlers",
    "EventBus",
    "JsonlSink",
    "NullSink",
    "UdpSink",
    "ValidationError",
]
