"""标准库 HTTP 适配器（零依赖兜底）。

存在的意义不是"再造一个 Web 框架"，而是**证明抽象是真的**：
同一套 `transport.handlers`，换一个适配器，路由、状态码、SSE 帧格式都不变。
所以 `tests/test_transport.py` 用同一组契约测试同时跑这两个适配器。

限制（诚实边界）：
- 只有 Python 3.10+ 自带的能力，没有 OpenAPI、没有中间件生态、没有 HTTP/2；
- 线程模型是 `ThreadingHTTPServer`，一个连接一个线程；
- SSE 用 `Connection: close` 收尾（无 Content-Length），客户端读到 EOF 即结束。
  这是能用且符合事件流语义的最小实现，但不是生产级网关。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from transport.handlers import ApiHandlers, ValidationError, sse_frame
from transport.http.static import try_load


_SESSION_PATH = re.compile(
    r"^/sessions/(?P<sid>[^/]+)(?:/(?P<action>messages|resume|stream))?$"
)

JsonResult = Tuple[str, int, Dict[str, Any]]
StreamResult = Tuple[str, int, Iterator[Tuple[str, Dict[str, Any]]]]


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("request body must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("request body must be a JSON object")
    return value


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_bytes(handler: BaseHTTPRequestHandler, content_type: str, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_stream(handler: BaseHTTPRequestHandler, events) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    for event, data in events:
        try:
            handler.wfile.write(sse_frame(event, data).encode("utf-8"))
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def dispatch(
    handlers: ApiHandlers,
    method: str,
    path: str,
    query: Dict[str, str],
    body: Dict[str, Any],
):
    """Route one request. Returns ('json', status, payload) or ('stream', 200, iter)."""

    if method == "GET" and path == "/health":
        status, payload = handlers.health()
        return "json", status, payload

    if method == "POST" and path == "/chats/import":
        status, payload = handlers.import_chat(body)
        return "json", status, payload

    if method == "POST" and path == "/sessions":
        status, payload = handlers.create_session(body)
        return "json", status, payload

    match = _SESSION_PATH.match(path)
    if match:
        session_id = unquote(match.group("sid"))
        action = match.group("action")
        if action is None and method == "GET":
            status, payload = handlers.get_session(session_id)
            return "json", status, payload
        if action is None and method == "DELETE":
            status, payload = handlers.delete_session(session_id)
            return "json", status, payload
        if action == "messages" and method == "POST":
            status, payload = handlers.post_message(session_id, body)
            return "json", status, payload
        if action == "resume" and method == "POST":
            status, payload = handlers.resume(session_id, body)
            return "json", status, payload
        if action == "stream" and method == "GET":
            status, payload = handlers.stream_events(session_id, query)
            if status != 200:
                return "json", status, payload
            return "stream", status, payload

    return "json", 404, {"detail": "not found"}


class StdlibHttpServer:
    """ThreadingHTTPServer wrapper with start/stop and an ephemeral-port mode."""

    def __init__(
        self,
        handlers: ApiHandlers,
        host: str = "127.0.0.1",
        port: int = 8000,
    ):
        self.handlers = handlers
        self._httpd = ThreadingHTTPServer((host, int(port)), self._build_handler())
        self._httpd.daemon_threads = True
        self.host = self._httpd.server_address[0]
        self.port = int(self._httpd.server_address[1])
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "StdlibHttpServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name="stdlib-http", daemon=True
            )
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def address(self) -> Tuple[str, int]:
        return self.host, self.port

    # -- request handling ------------------------------------------------
    def _build_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "PersonaReplayStdlib/0.9"

            def log_message(self, fmt: str, *args) -> None:  # noqa: D401
                return  # 静默：日志走 EventBus，不在这里打印

            def do_GET(self) -> None:  # noqa: N802
                outer._dispatch(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                outer._dispatch(self, "POST")

            def do_DELETE(self) -> None:  # noqa: N802
                outer._dispatch(self, "DELETE")

        return Handler

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlparse(handler.path)
        query = {key: values[0] if values else "" for key, values in parse_qs(parsed.query).items()}
        try:
            body = _read_json_body(handler) if method in ("POST", "PUT", "PATCH") else {}
        except ValidationError as exc:
            _write_json(handler, 422, {"detail": str(exc)})
            return

        # 静态页优先：GET 且能解析到 web/ 下的文件就直接返回，不进入 API 路由。
        if method == "GET":
            loaded = try_load(parsed.path)
            if loaded is not None:
                content_type, payload = loaded
                _write_bytes(handler, content_type, payload)
                return

        try:
            kind, status, payload = dispatch(
                self.handlers, method, parsed.path, query, body
            )
        except ValidationError as exc:
            _write_json(handler, 422, {"detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要给出响应
            _write_json(handler, 500, {"detail": f"internal error: {exc}"})
            return

        if kind == "stream":
            _write_stream(handler, payload)
        else:
            _write_json(handler, status, payload)


def create_server(
    handlers: ApiHandlers,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> StdlibHttpServer:
    return StdlibHttpServer(handlers, host=host, port=port)
