"""trace 标识：一轮对话的事件要能串起来，且字段名对齐 OTel。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.engine import create_runtime
from observability.trace import new_span_id, new_trace_id, trace_fields
from transport.events import EventBus, JsonlSink

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")


class TraceFieldTests(unittest.TestCase):
    def test_id_shapes(self) -> None:
        self.assertEqual(len(new_trace_id()), 32)
        self.assertEqual(len(new_span_id()), 16)
        int(new_trace_id(), 16)  # 必须是合法 hex
        int(new_span_id(), 16)

    def test_ids_are_unique(self) -> None:
        self.assertEqual(len({new_trace_id() for _ in range(50)}), 50)
        self.assertEqual(len({new_span_id() for _ in range(50)}), 50)

    def test_trace_fields_have_both_naming_schemes(self) -> None:
        fields = trace_fields("a" * 32, "b" * 16)
        self.assertEqual(fields["trace_id"], "a" * 32)
        self.assertEqual(fields["traceId"], "a" * 32)
        self.assertEqual(fields["parentSpanId"], "b" * 16)
        self.assertEqual(fields["parent_span_id"], "b" * 16)
        self.assertEqual(len(fields["span_id"]), 16)


class EventTraceTests(unittest.TestCase):
    def test_events_from_same_state_share_trace(self) -> None:
        bus = EventBus()
        state = {
            "trace_id": "c" * 32,
            "root_span_id": "d" * 16,
            "status": "ok",
            "turn_count": 1,
        }
        first = bus._build("persona", state)
        second = bus._build("retrieve", state)
        self.assertEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(first["parent_span_id"], second["parent_span_id"])
        self.assertNotEqual(first["span_id"], second["span_id"])

    def test_events_without_trace_stay_plain(self) -> None:
        event = EventBus()._build("persona", {"status": "ok"})
        self.assertNotIn("trace_id", event)

    def test_jsonl_sink_writes_otel_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            sink = JsonlSink(path)
            bus = EventBus(sinks=[sink])
            try:
                bus.emit(
                    "draft",
                    {"trace_id": "e" * 32, "root_span_id": "f" * 16, "status": "ok"},
                )
            finally:
                sink.close()
            line = json.loads(path.read_text(encoding="utf-8").strip())
        self.assertEqual(line["traceId"], "e" * 32)
        self.assertEqual(line["parentSpanId"], "f" * 16)
        self.assertEqual(line["event"], "draft")


class RealTurnTraceTests(unittest.TestCase):
    def test_one_turn_emits_events_with_one_trace(self) -> None:
        seen = []
        bus = EventBus(sinks=[])
        bus._build = (  # type: ignore[method-assign]
            lambda name, state: seen.append(bus.__class__._build(bus, name, state)) or None
        )
        runtime = create_runtime(":memory:", engine="builtin")
        try:
            runtime.invoke(
                thread_id="trace-turn",
                chat_path=FIXTURE,
                user_speaker="老王",
                target_speaker="小林",
                user_input="王哥今晚加班，晚饭你自己先吃？",
                on_event=bus,
            )
        finally:
            runtime.close()

        traces = {event["trace_id"] for event in seen if "trace_id" in event}
        self.assertEqual(len(traces), 1, f"应只有一个 trace，实际 {traces}")
        span_ids = [event["span_id"] for event in seen if "span_id" in event]
        self.assertEqual(len(span_ids), len(set(span_ids)), "每个事件的 span_id 必须唯一")


if __name__ == "__main__":
    unittest.main()
