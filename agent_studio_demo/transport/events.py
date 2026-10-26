"""观测事件通道（UDP 侧）。

为什么观测走 UDP 而不是 HTTP：
- 每轮扮演会产生 4–9 个节点事件，用 HTTP 上报会拖慢主链路、还要处理超时和重试；
- 观测数据**丢了不影响正确性**，适合 fire-and-forget；
- `sendto` 系统调用几乎不阻塞，且天然不反压业务。

反过来，UDP 也**绝不能**承载业务数据：无序、可丢、无连接。
HITL 裁决、回复正文、checkpoint 都必须走 TCP。

隐私：`EventBus` 构造的事件里**没有对话内容**——只有长度、命中数、判定结果。
这样即使 UDP 包被旁路，也不会泄露聊天原文。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol


class EventSink(Protocol):
    name: str

    def emit(self, event: Dict[str, Any]) -> None:
        ...


class NullSink:
    """Black hole: the default when nothing is configured."""

    name = "null"

    def emit(self, event: Dict[str, Any]) -> None:  # noqa: D401 - protocol impl
        return None


class JsonlSink:
    """Append-only JSON lines, for local debugging."""

    name = "jsonl"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")

    def emit(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            self._handle.write(line)
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


class UdpSink:
    """Fire-and-forget datagram sink (statsd-style, one JSON per packet)."""

    name = "udp"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8125,
        max_bytes: int = 8192,
        timeout: float = 0.0,
    ):
        if not 0 < port < 65536:
            raise ValueError("port must be between 1 and 65535")
        self.address = (host, port)
        self.max_bytes = max_bytes
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(timeout or None)

    def emit(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        if len(payload) > self.max_bytes:
            # 超长就截断而不是分片：观测数据不值得为了完整性引入拆包逻辑。
            payload = payload[: self.max_bytes]
        self._socket.sendto(payload, self.address)

    def close(self) -> None:
        self._socket.close()


class EventBus:
    """Graph/Service 的事件回调：`(name, state) -> None`。

    签名故意和 `agent.graph.EventListener` 一致，所以可以直接当 `on_event` 传进去。
    """

    def __init__(
        self,
        sinks: Optional[Iterable[EventSink]] = None,
        clock=time.perf_counter,
    ):
        sinks = list(sinks or [])
        self.sinks: List[EventSink] = sinks or [NullSink()]
        self.dropped = 0
        self._clock = clock
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    # -- EventListener ---------------------------------------------------
    def __call__(self, name: str, state: dict) -> None:
        self.emit(name, state)

    # -- API -------------------------------------------------------------
    def emit(self, name: str, state: dict) -> None:
        event = self._build(name, state)
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception:
                # 观测失败不能影响主流程：计数 + 继续。
                with self._lock:
                    self.dropped += 1

    def _build(self, name: str, state: dict) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            delta = None if self._last is None else round((now - self._last) * 1000, 3)
            self._last = now

        critique = state.get("critique") or {}
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": str(name),
            # 边界到上一个事件的间隔 ≈ 上一个节点的耗时（emit 在节点返回之后）。
            "ms_since_prev": delta,
            "status": state.get("status"),
            "turn": state.get("turn_count"),
            "rewrites": state.get("rewrite_count"),
            "hits": len(state.get("retrieved") or []),
            "reply_len": len(str(state.get("reply") or "")),
            "verdict": critique.get("verdict"),
            "problems": list(critique.get("problems") or []),
            "steps": list(state.get("steps") or []),
        }

    def close(self) -> None:
        for sink in self.sinks:
            closer = getattr(sink, "close", None)
            if callable(closer):
                closer()
