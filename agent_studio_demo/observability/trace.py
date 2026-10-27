"""Trace 标识（W3C Trace Context 风格，零依赖）。

为什么需要：现在的观测事件只有 `ts + event`，一轮对话的 4–9 个事件**串不起来**。
加上 `trace_id`（整轮共享）+ `span_id`（每个事件唯一）+ `parent_span_id`（指向本轮
根 span）之后：
- 本地能看到"这一轮都发生了什么"，而不是一堆散装事件；
- JSONL 直接对齐 OTel 的字段名，将来接 Langfuse / Phoenix / Jaeger 不用改埋点。

id 格式按 W3C：trace 32 个 hex，span 16 个 hex。不用 `uuid.uuid4()` 是因为它的
hex 是 32 位没错，但 span 需要独立的 16 位；而且 `secrets` 不依赖 uuid 的版本语义。
"""

from __future__ import annotations

import secrets


def new_trace_id() -> str:
    """32 hex：一轮对话共享。"""

    return secrets.token_hex(16)


def new_span_id() -> str:
    """16 hex：一个事件/节点一个。"""

    return secrets.token_hex(8)


def trace_fields(trace_id: str, root_span_id: str, span_id: str | None = None) -> dict:
    """同时给出自有字段名和 OTel 字段名，方便人看也方便机器读。"""

    span = span_id or new_span_id()
    return {
        "trace_id": trace_id,
        "span_id": span,
        "parent_span_id": root_span_id,
        # OTel/JSON 侧的别名（导出时直接可用）
        "traceId": trace_id,
        "spanId": span,
        "parentSpanId": root_span_id,
    }


__all__ = ["new_span_id", "new_trace_id", "trace_fields"]
