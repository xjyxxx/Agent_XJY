"""传输层：事件通道、适配器等价性、TCP+UDP 组合。

最有价值的一组是 `ContractTests`：**同一套断言同时跑在 FastAPI 适配器与标准库
适配器上**。如果哪天有人把业务逻辑写进某个适配器里，这组测试会立刻红。
"""

import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.service import SessionService
from ingest import session_store
from transport.bundle import Transport, resolve_adapter, resolve_server
from transport.events import EventBus, JsonlSink, NullSink, UdpSink
from transport.handlers import ApiHandlers, ValidationError
from transport.http.stdlib_adapter import StdlibHttpServer, dispatch

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


# ---------------------------------------------------------------- helpers
def _module_available(name: str) -> bool:
    """真 import 一次，而不是 find_spec —— 这样"模拟缺依赖"的测试也能生效。"""

    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


class _Response:
    def __init__(self, status: int, body: str, headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class UrllibClient:
    """极简客户端，接口形状和 starlette 的 TestClient 对齐。"""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def _request(self, method: str, path: str, payload=None) -> _Response:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return _Response(
                    response.status,
                    response.read().decode("utf-8"),
                    {key.lower(): value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            return _Response(
                exc.code,
                exc.read().decode("utf-8"),
                {key.lower(): value for key, value in (exc.headers or {}).items()},
            )

    def get(self, path: str) -> _Response:
        return self._request("GET", path)

    def post(self, path: str, payload=None) -> _Response:
        return self._request("POST", path, payload if payload is not None else {})

    def delete(self, path: str) -> _Response:
        return self._request("DELETE", path)


class TestClientAdapter:
    def __init__(self, client):
        self._client = client

    def get(self, path: str):
        return self._client.get(path)

    def post(self, path: str, payload=None):
        return self._client.post(path, json=payload if payload is not None else {})

    def delete(self, path: str):
        return self._client.delete(path)


def new_service() -> SessionService:
    session_store.clear()
    return SessionService(":memory:")


@contextmanager
def stdlib_transport(service: SessionService, bus=None):
    handlers = ApiHandlers(service, bus or EventBus([NullSink()]))
    server = StdlibHttpServer(handlers, host="127.0.0.1", port=0).start()
    try:
        yield UrllibClient(f"http://{server.host}:{server.port}")
    finally:
        server.shutdown()


def exercise_contract(test: unittest.TestCase, client) -> None:
    """两个适配器都必须满足的行为契约。"""

    test.assertEqual(client.get("/health").json()["status"], "ok")

    imported = client.post(
        "/chats/import",
        {"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"},
    )
    test.assertEqual(imported.status_code, 200)
    test.assertEqual(imported.json()["speakers"], ["老王", "小林"])

    created = client.post(
        "/sessions", {"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"}
    )
    test.assertEqual(created.status_code, 201)
    session_id = created.json()["session_id"]
    test.assertEqual(created.json()["persona"]["speaker"], "小林")

    turn = client.post(f"/sessions/{session_id}/messages", {"message": GOLDEN_QUERY})
    test.assertEqual(turn.status_code, 200)
    body = turn.json()
    test.assertEqual(body["reply"], "哈哈知道了")
    test.assertEqual(body["critique"]["verdict"], "pass")
    test.assertEqual(body["retrieved"][0]["line_number"], 15)

    test.assertEqual(client.get(f"/sessions/{session_id}").json()["turn_count"], 1)

    test.assertEqual(client.get("/sessions/nope").status_code, 404)
    test.assertEqual(
        client.post("/sessions/nope/messages", {"message": "hi"}).status_code, 404
    )
    test.assertEqual(
        client.post(f"/sessions/{session_id}/resume", {"action": "approve"}).status_code,
        409,
    )
    test.assertEqual(
        client.post(f"/sessions/{session_id}/resume", {"action": "maybe"}).status_code,
        422,
    )
    test.assertEqual(
        client.post(f"/sessions/{session_id}/messages", {"message": ""}).status_code, 422
    )

    test.assertEqual(client.delete(f"/sessions/{session_id}").status_code, 200)
    test.assertEqual(client.get(f"/sessions/{session_id}").status_code, 404)


# ---------------------------------------------------------------- unit tests
class EventTests(unittest.TestCase):
    def test_bus_fans_out_without_leaking_content(self) -> None:
        seen = []

        class Collector:
            name = "collector"

            def emit(self, event):
                seen.append(event)

        bus = EventBus([Collector()])
        bus("draft", {"reply": "这是绝对不能进遥测的原文", "status": "ok", "turn_count": 1})
        self.assertEqual(len(seen), 1)
        event = seen[0]
        self.assertEqual(event["event"], "draft")
        self.assertIn("ms_since_prev", event)
        self.assertNotIn("reply", event)
        self.assertNotIn("这是绝对不能进遥测的原文", json.dumps(event, ensure_ascii=False))

    def test_broken_sink_does_not_break_the_turn(self) -> None:
        good = []

        class Broken:
            name = "broken"

            def emit(self, event):
                raise OSError("boom")

        class Collector:
            name = "ok"

            def emit(self, event):
                good.append(event)

        bus = EventBus([Broken(), Collector()])
        bus("critic", {"status": "ok"})
        self.assertEqual(len(good), 1)
        self.assertEqual(bus.dropped, 1)

    def test_udp_sink_delivers_datagrams(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(3)
        port = receiver.getsockname()[1]
        try:
            sink = UdpSink("127.0.0.1", port)
            bus = EventBus([sink])
            bus("retrieve", {"status": "ok", "retrieved": [{"content": "x"}]})
            payload, _ = receiver.recvfrom(8192)
            event = json.loads(payload.decode("utf-8"))
            self.assertEqual(event["event"], "retrieve")
            self.assertEqual(event["hits"], 1)
            sink.close()
        finally:
            receiver.close()

    def test_udp_sink_truncates_oversized_events(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(3)
        port = receiver.getsockname()[1]
        try:
            sink = UdpSink("127.0.0.1", port, max_bytes=64)
            sink.emit({"event": "x", "steps": ["a" * 500]})
            payload, _ = receiver.recvfrom(8192)
            self.assertLessEqual(len(payload), 64)
            sink.close()
        finally:
            receiver.close()

    def test_jsonl_sink_appends_lines(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            sink = JsonlSink(path)
            sink.emit({"event": "a"})
            sink.emit({"event": "b"})
            sink.close()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual([json.loads(line)["event"] for line in lines], ["a", "b"])


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handlers = ApiHandlers(new_service())

    def test_validation_errors_are_marked_for_422(self) -> None:
        with self.assertRaises(ValidationError):
            self.handlers.import_chat({})
        with self.assertRaises(ValidationError):
            self.handlers.post_message("whatever", {"message": ""})
        with self.assertRaises(ValidationError):
            self.handlers.resume("whatever", {"action": "maybe"})

    def test_domain_errors_are_mapped(self) -> None:
        self.assertEqual(self.handlers.import_chat({"path": "nope.txt"})[0], 400)
        self.assertEqual(self.handlers.get_session("missing")[0], 404)

    def test_dispatch_unknown_route(self) -> None:
        kind, status, payload = dispatch(self.handlers, "GET", "/nope", {}, {})
        self.assertEqual((kind, status), ("json", 404))
        self.assertIn("detail", payload)


class ContractTests(unittest.TestCase):
    """两个适配器跑同一套契约。"""

    def test_stdlib_adapter_contract(self) -> None:
        with stdlib_transport(new_service()) as client:
            exercise_contract(self, client)

    def test_fastapi_adapter_contract(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:  # pragma: no cover
            self.skipTest("可选依赖未安装")

        from transport.http.fastapi_adapter import create_app

        service = new_service()
        client = TestClientAdapter(
            TestClient(create_app(checkpoint_path=":memory:", service=service))
        )
        exercise_contract(self, client)


class StreamingTests(unittest.TestCase):
    def test_stdlib_sse_frames_and_order(self) -> None:
        with stdlib_transport(new_service()) as client:
            session_id = client.post(
                "/sessions",
                {"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"},
            ).json()["session_id"]

            body = client.get(
                f"/sessions/{session_id}/stream?message={urllib.request.quote(GOLDEN_QUERY)}"
            ).text

            events = [
                line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")
            ]
            self.assertEqual(events[0], "persona")
            self.assertLess(events.index("retrieved"), events.index("draft"))
            self.assertLess(events.index("draft"), events.index("critique"))
            self.assertLess(events.index("critique"), events.index("reply_delta"))
            self.assertEqual(events[-1], "done")

    def test_stdlib_stream_unknown_session_is_404(self) -> None:
        with stdlib_transport(new_service()) as client:
            self.assertEqual(client.get("/sessions/nope/stream?message=hi").status_code, 404)


class StaticAndSandboxTests(unittest.TestCase):
    """前端静态资源 + 导入路径白名单。"""

    def test_index_and_assets_are_served(self) -> None:
        with stdlib_transport(new_service()) as client:
            index = client.get("/")
            self.assertEqual(index.status_code, 200)
            self.assertIn("Persona Replay", index.text)
            self.assertIn("text/html", self._content_type(client, "/"))

            for asset, expected in (("/app.js", "javascript"), ("/style.css", "text/css")):
                response = client.get(asset)
                self.assertEqual(response.status_code, 200, asset)
                self.assertIn(expected, self._content_type(client, asset))
            self.assertIn("fetch", client.get("/app.js").text)

    def _content_type(self, client, path: str) -> str:
        return client.get(path).headers.get("content-type", "")

    def test_unknown_static_path_is_404(self) -> None:
        with stdlib_transport(new_service()) as client:
            self.assertEqual(client.get("/nope.js").status_code, 404)
            self.assertEqual(client.get("/nope").status_code, 404)

    def test_path_traversal_is_refused(self) -> None:
        from transport.http.static import StaticError, load

        for attempt in ("/../.env", "/..%2f.env", "/%2e%2e/.env", "/../README.md"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(StaticError):
                    load(attempt)
        with stdlib_transport(new_service()) as client:
            # 服务端不会把 .env 吐出来
            self.assertEqual(client.get("/../.env").status_code, 404)
            self.assertNotIn("DEEPSEEK_API_KEY", client.get("/../.env").text)

    def test_import_path_must_stay_inside_chat_root(self) -> None:
        with stdlib_transport(new_service()) as client:
            for attempt in ("C:/Windows/win.ini", "/etc/passwd", "../../secrets.txt"):
                with self.subTest(path=attempt):
                    response = client.post("/chats/import", {"path": attempt})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("CHAT_ROOT", response.json()["detail"])

            inside = client.post(
                "/chats/import",
                {"path": str(FIXTURE)},
            )
            self.assertEqual(inside.status_code, 200)

    @unittest.skipUnless(
        _module_available("fastapi"), "可选依赖未安装（pip install -r requirements-optional.txt）"
    )
    def test_fastapi_adapter_also_serves_the_frontend(self) -> None:
        import sys as _sys

        _sys.path.insert(0, str(PROJECT_ROOT))
        from fastapi.testclient import TestClient

        from transport.http.fastapi_adapter import create_app

        client = TestClient(create_app(checkpoint_path=":memory:", service=new_service()))
        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("Persona Replay", index.text)
        self.assertEqual(client.get("/app.js").status_code, 200)
        self.assertEqual(client.get("/nope.js").status_code, 404)


class BundleTests(unittest.TestCase):
    def test_adapter_resolution(self) -> None:
        self.assertEqual(resolve_adapter("stdlib"), "stdlib")
        self.assertIn(resolve_adapter("auto"), {"stdlib", "fastapi"})

    def test_server_resolution_rules(self) -> None:
        self.assertEqual(resolve_server("auto", "stdlib"), "stdlib")
        self.assertEqual(resolve_server("uvicorn", "fastapi"), "uvicorn")
        self.assertEqual(resolve_server("granian", "fastapi"), "granian")
        with self.assertRaisesRegex(ValueError, "only supports server=stdlib"):
            resolve_server("granian", "stdlib")
        with self.assertRaisesRegex(ValueError, "cannot run on the stdlib server"):
            resolve_server("stdlib", "fastapi")
        with self.assertRaisesRegex(ValueError, "unknown server"):
            resolve_server("hypercorn", "fastapi")

    def test_all_server_backends_serve_the_same_turn(self) -> None:
        """同一套 handler，换 server 后端，行为必须一致。"""

        backends = [("stdlib", "stdlib")]
        if _module_available("uvicorn"):
            backends.append(("fastapi", "uvicorn"))
        if _module_available("granian"):
            backends.append(("fastapi", "granian"))

        for adapter, server in backends:
            with self.subTest(server=server):
                transport = Transport(
                    tcp_port=0,
                    adapter=adapter,
                    server=server,
                    checkpoint_path=":memory:",
                )
                try:
                    info = transport.start()
                    self.assertEqual(info.server, server)
                    client = UrllibClient(f"http://{info.tcp[0]}:{info.tcp[1]}")
                    self.assertEqual(client.get("/health").json()["status"], "ok")

                    created = client.post(
                        "/sessions",
                        {
                            "path": FIXTURE,
                            "user_speaker": "老王",
                            "target_speaker": "小林",
                        },
                    )
                    self.assertEqual(created.status_code, 201, created.text)
                    session_id = created.json()["session_id"]

                    turn = client.post(
                        f"/sessions/{session_id}/messages", {"message": GOLDEN_QUERY}
                    )
                    self.assertEqual(turn.status_code, 200, turn.text)
                    self.assertEqual(turn.json()["reply"], "哈哈知道了")
                finally:
                    transport.close()

    def test_tcp_and_udp_share_one_lifecycle(self) -> None:
        """一个进程里：HTTP 走 TCP，观测走 UDP，两者同一个 start/stop。"""

        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        udp_port = receiver.getsockname()[1]

        transport = Transport(
            service=new_service(),
            host="127.0.0.1",
            tcp_port=0,
            udp_port=udp_port,
            adapter="stdlib",
            checkpoint_path=":memory:",
        )
        try:
            info = transport.start()
            self.assertEqual(info.adapter, "stdlib")
            self.assertEqual(info.udp, ("127.0.0.1", udp_port))

            client = UrllibClient(f"http://{info.tcp[0]}:{info.tcp[1]}")
            session_id = client.post(
                "/sessions",
                {"path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"},
            ).json()["session_id"]
            client.post(f"/sessions/{session_id}/messages", {"message": GOLDEN_QUERY})

            received = []
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    payload, _ = receiver.recvfrom(8192)
                except socket.timeout:
                    break
                received.append(json.loads(payload.decode("utf-8")))
                if received and received[-1]["event"] in {"critic", "done"}:
                    break

            names = [event["event"] for event in received]
            self.assertIn("retrieve", names)
            self.assertIn("critic", names)
            # 观测事件里不能出现对话原文
            blob = json.dumps(received, ensure_ascii=False)
            self.assertNotIn("哈哈知道了", blob)
            self.assertNotIn(GOLDEN_QUERY, blob)
        finally:
            transport.stop()
            transport.close()
            receiver.close()

    def test_transport_stop_is_idempotent(self) -> None:
        transport = Transport(
            service=new_service(),
            tcp_port=0,
            adapter="stdlib",
            checkpoint_path=":memory:",
        )
        transport.start()
        transport.stop()
        transport.stop()
        transport.close()


if __name__ == "__main__":
    unittest.main()
