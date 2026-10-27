"""观测能力（trace 标识）。零依赖，可选接 OTel 导出。"""

from observability.trace import new_span_id, new_trace_id, trace_fields

__all__ = ["new_span_id", "new_trace_id", "trace_fields"]
