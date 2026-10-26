"""本地假 DeepSeek：实现 OpenAI 兼容的 /chat/completions（JSON + SSE）。

为什么需要它：真实 API 需要用户的 key，AI 不能替你验证。
但"挂载远程模型"这件事的关键是**协议层**——请求体、鉴权头、SSE 帧解析、
错误码映射。这些都能用一个同协议的本地服务验证；等你填上 key，同一段代码直接可用。

用法（测试里）：
    with FakeDeepSeek(reply="哈哈知道了") as fake:
        model = DeepSeekReplyModel(api_key="sk-test", base_url=fake.base_url, model="deepseek-flash")
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional


class FakeDeepSeek:
    def __init__(
        self,
        reply: str = "哈哈知道了",
        chunk_size: int = 2,
        fail_status: Optional[int] = None,
        require_auth: bool = True,
        expected_token: str = "sk-test",
    ):
        self.reply = reply
        self.chunk_size = max(1, chunk_size)
        self.fail_status = fail_status
        self.require_auth = require_auth
        self.expected_token = expected_token
        self.requests: List[dict] = []
        self.stream_requests = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "FakeDeepSeek":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-deepseek", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeDeepSeek":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- protocol --------------------------------------------------------
    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                return

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if self.path != "/chat/completions":
                    self._json(404, {"error": "not found"})
                    return

                if outer.fail_status:
                    self._json(outer.fail_status, {"error": {"message": "boom"}})
                    return

                if outer.require_auth:
                    token = (self.headers.get("Authorization") or "").replace(
                        "Bearer ", ""
                    )
                    if token != outer.expected_token:
                        self._json(401, {"error": {"message": "invalid api key"}})
                        return

                try:
                    request = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._json(400, {"error": {"message": "bad json"}})
                    return
                outer.requests.append(request)

                if request.get("stream"):
                    outer.stream_requests += 1
                    self._stream(request)
                else:
                    self._json(
                        200,
                        {
                            "id": "fake-1",
                            "model": request.get("model", "deepseek-flash"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": outer.reply,
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    )

            def _stream(self, request: dict) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                text = outer.reply
                for index in range(0, len(text), outer.chunk_size):
                    piece = text[index : index + outer.chunk_size]
                    frame = {
                        "id": "fake-1",
                        "model": request.get("model", "deepseek-flash"),
                        "choices": [{"index": 0, "delta": {"content": piece}}],
                    }
                    self.wfile.write(
                        f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler
