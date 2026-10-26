"""传输组合：一个进程同时提供 TCP 服务与 UDP 上报。

两个维度，互相正交：

    adapter（框架）: fastapi（可选依赖） | stdlib（零依赖）
    server （服务器）: uvicorn | granian | stdlib

    TCP：HTTP 请求/响应 + SSE（不能丢的东西）
    UDP：节点级观测事件（丢了不影响正确性）

两者共享同一个 `EventBus`，所以"服务在跑"和"事件在发"是同一个生命周期。

关于 granian：它是 Rust 实现的 ASGI 服务器，性能明显高于 uvicorn，但它会在
**主线程**注册信号处理器，线程内 `serve()` 会直接抛
`ValueError: signal only works in main thread`。所以这里用**子进程**起它，
配置通过环境变量传递（见 `transport/http/app.py`）。这也更接近真实部署方式。
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from api.service import SessionService
from llm_client.factory import create_reply_model, load_dotenv
from transport.events import EventBus, JsonlSink, UdpSink
from transport.handlers import ApiHandlers


ADAPTERS = ("auto", "fastapi", "stdlib")
SERVERS = ("auto", "uvicorn", "granian", "stdlib")
STARTUP_TIMEOUT_SECONDS = 20


@dataclass
class TransportInfo:
    adapter: str
    server: str
    tcp: Optional[Tuple[str, int]]
    udp: Optional[Tuple[str, int]]
    jsonl: Optional[str] = None
    dropped_events: int = 0

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "server": self.server,
            "tcp": list(self.tcp) if self.tcp else None,
            "udp": list(self.udp) if self.udp else None,
            "jsonl": self.jsonl,
            "dropped_events": self.dropped_events,
        }


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def resolve_adapter(name: str = "auto") -> str:
    if name in ("fastapi", "stdlib"):
        return name
    if name != "auto":
        raise ValueError(f"unknown adapter: {name!r} (auto|fastapi|stdlib)")
    from transport.http.fastapi_adapter import http_stack_available

    return "fastapi" if http_stack_available() else "stdlib"


def _provider_hint(model) -> Optional[str]:
    """把模型实例映射回 factory 能识别的 provider 名。

    granian 跑在子进程里，拿不到父进程的模型对象，只能通过环境变量传递"用哪个模型"；
    如果这里不传，子进程会按 `PROVIDER=auto` 自己选，于是"父进程用离线模型、子进程
    用远程模型"，行为和断言都会飘。
    """

    name = type(model).__name__ if model is not None else ""
    if name == "HeuristicReplyModel":
        return "heuristic"
    if name == "DeepSeekReplyModel":
        return "deepseek"
    return None


def resolve_server(name: str = "auto", adapter: str = "fastapi") -> str:
    """Granian > uvicorn > stdlib：快的那层优先，但必须已安装。"""

    if adapter == "stdlib":
        if name not in ("auto", "stdlib"):
            raise ValueError(f"adapter=stdlib only supports server=stdlib, got {name!r}")
        return "stdlib"

    if name == "stdlib":
        raise ValueError("adapter=fastapi cannot run on the stdlib server; use adapter=stdlib")
    if name in ("uvicorn", "granian"):
        return name
    if name != "auto":
        raise ValueError(f"unknown server: {name!r} (auto|uvicorn|granian|stdlib)")

    if _has("granian"):
        return "granian"
    if _has("uvicorn"):
        return "uvicorn"
    return "stdlib"


def _free_port(host: str = "127.0.0.1") -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def wait_for_port(host: str, port: int, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.25):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class Transport:
    def __init__(
        self,
        service: Optional[SessionService] = None,
        host: str = "127.0.0.1",
        tcp_port: int = 8000,
        udp_port: Optional[int] = None,
        jsonl_path: Optional[str] = None,
        adapter: str = "auto",
        server: str = "auto",
        model=None,
        max_steps: int = 12,
        checkpoint_path: Optional[str] = None,
    ):
        load_dotenv()
        self.host = host
        self.udp_port = int(udp_port) if udp_port else None
        self.jsonl_path = jsonl_path
        self.adapter_name = resolve_adapter(adapter)
        self.server_name = resolve_server(server, self.adapter_name)
        self.tcp_port = int(tcp_port)
        self.checkpoint_path = checkpoint_path or "runtime/api.sqlite3"
        self._stop_event = threading.Event()

        self._owns_service = service is None
        self.service = service or SessionService(
            self.checkpoint_path,
            model=model
            if model is not None
            else create_reply_model(os.environ.get("PROVIDER", "auto")),
            max_steps=max_steps,
        )

        sinks = []
        if self.udp_port:
            sinks.append(UdpSink(host=host, port=self.udp_port))
        if jsonl_path:
            sinks.append(JsonlSink(jsonl_path))
        self.bus = EventBus(sinks)
        self.handlers = ApiHandlers(self.service, self.bus)

        self._stdlib_server = None
        self._uvicorn = None
        self._uvicorn_thread: Optional[threading.Thread] = None
        self._granian_proc: Optional[subprocess.Popen] = None
        self._granian_log = tempfile.TemporaryFile()
        self.info: Optional[TransportInfo] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> TransportInfo:
        if self.info is not None:
            return self.info

        if self.adapter_name == "stdlib":
            tcp = self._start_stdlib()
        elif self.server_name == "granian":
            tcp = self._start_granian()
        else:
            tcp = self._start_uvicorn()

        self.info = TransportInfo(
            adapter=self.adapter_name,
            server=self.server_name,
            tcp=tcp,
            udp=(self.host, self.udp_port) if self.udp_port else None,
            jsonl=next(
                (
                    getattr(sink, "path", None)
                    for sink in self.bus.sinks
                    if sink.name == "jsonl"
                ),
                None,
            ),
        )
        return self.info

    def _start_stdlib(self) -> Tuple[str, int]:
        from transport.http.stdlib_adapter import StdlibHttpServer

        self._stdlib_server = StdlibHttpServer(
            self.handlers, host=self.host, port=self.tcp_port
        ).start()
        return self._stdlib_server.address

    def _start_uvicorn(self) -> Tuple[str, int]:
        import uvicorn

        from transport.http.fastapi_adapter import create_app

        app = create_app(service=self.service, bus=self.bus)
        config = uvicorn.Config(
            app, host=self.host, port=self.tcp_port, log_level="warning"
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        self._uvicorn_thread = threading.Thread(
            target=server.run, name="uvicorn", daemon=True
        )
        self._uvicorn_thread.start()

        deadline = time.time() + STARTUP_TIMEOUT_SECONDS
        while not server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start within 20s")
            if self._uvicorn_thread and not self._uvicorn_thread.is_alive():
                raise RuntimeError("uvicorn thread exited during startup")
            time.sleep(0.02)

        port = self.tcp_port
        try:
            port = int(server.servers[0].sockets[0].getsockname()[1])
        except (AttributeError, IndexError):  # pragma: no cover - uvicorn 内部结构变化
            pass
        return self.host, port

    def _start_granian(self) -> Tuple[str, int]:
        if self.tcp_port == 0:
            # granian 是子进程，父进程读不到它的 ephemeral 端口，所以先自己挑一个。
            self.tcp_port = _free_port(self.host)

        env = {
            **os.environ,
            "CHECKPOINT_PATH": self.checkpoint_path,
            # 子进程必须用和父进程相同的模型，否则断言会随本机 .env 飘。
            "PROVIDER": _provider_hint(getattr(self.service.runtime, "model", None))
            or os.environ.get("PROVIDER", "auto"),
            "UDP_HOST": self.host,
            "UDP_PORT": str(self.udp_port or 0),
            "TRACE_JSONL": self.jsonl_path or "",
        }
        command = [
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--factory",
            "--host",
            self.host,
            "--port",
            str(self.tcp_port),
            "--workers",
            "1",
            "--log-level",
            "warning",
            "transport.http.app:create_app",
        ]
        self._granian_proc = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            # 用临时文件而不是 PIPE：PIPE 不读会填满 64KB 缓冲把子进程卡死，
            # 而且退出时不 close 会触发 ResourceWarning。
            stdout=self._granian_log,
            stderr=self._granian_log,
        )
        if not wait_for_port(self.host, self.tcp_port):
            self._granian_log.seek(0)
            detail = self._granian_log.read().decode("utf-8", "replace")[:500]
            self._granian_proc.terminate()
            raise RuntimeError(
                f"granian did not start within {STARTUP_TIMEOUT_SECONDS}s: {detail}"
            )
        return self.host, self.tcp_port

    def wait(self, timeout: Optional[float] = None) -> None:
        """Block until stop() is called (or timeout)."""

        self._stop_event.wait(timeout)

    def stop(self) -> None:
        if self.info is None:
            return
        self._stop_event.set()

        if self._stdlib_server is not None:
            self._stdlib_server.shutdown()
            self._stdlib_server = None

        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
            if self._uvicorn_thread is not None:
                self._uvicorn_thread.join(timeout=5)
            self._uvicorn = None
            self._uvicorn_thread = None

        if self._granian_proc is not None:
            self._granian_proc.terminate()
            try:
                self._granian_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._granian_proc.kill()
                self._granian_proc.wait(timeout=5)
            self._granian_proc = None
            try:
                self._granian_log.close()
            except OSError:
                pass

        self.info.dropped_events = self.bus.dropped

    def close(self) -> None:
        self.stop()
        # 即使从没起过 granian，也可能已经创建了临时日志文件，必须关掉。
        try:
            self._granian_log.close()
        except OSError:
            pass
        self.bus.close()
        self.service.close()

    def __enter__(self) -> "Transport":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def serve_forever(**kwargs) -> None:
    """Start TCP + UDP, print the endpoints, block until Ctrl+C."""

    transport = Transport(**kwargs)
    info = transport.start()
    # flush=True：重定向到管道时 stdout 是块缓冲，启动信息不 flush 会"看起来没起来"。
    print(f"adapter: {info.adapter}  server: {info.server}", flush=True)
    print("tcp:", f"http://{info.tcp[0]}:{info.tcp[1]}" if info.tcp else "off", flush=True)
    print("udp:", f"{info.udp[0]}:{info.udp[1]}" if info.udp else "off", flush=True)
    if info.jsonl:
        print("jsonl:", info.jsonl, flush=True)
    try:
        transport.wait()
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
