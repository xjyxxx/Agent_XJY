"""一键构建与运行（跨平台）。

    python manage.py build          # 语法编译 + 导入冒烟 + 依赖检查
    python manage.py test           # 跑全部单测
    python manage.py run            # 构建检查后启动服务，并打开浏览器
    python manage.py all            # build + test + run

为什么用 Python 而不是 .ps1/.sh：Windows / macOS / Linux 一套命令，
不用处理引号差异；`run.ps1` 只是给 Windows 用户的快捷方式。
"""

from __future__ import annotations

import argparse
import compileall
import importlib
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_PY = (3, 10)
OPTIONAL_MODULES = ("fastapi", "uvicorn", "pydantic", "granian", "langgraph")

SMOKE_IMPORTS = (
    "agent.graph",
    "agent.nodes",
    "ingest.session_store",
    "memory.persona",
    "memory.checkpoint",
    "rag.history_index",
    "rag.retriever",
    "tools.chat_parser",
    "evaluator.llm_judge",
    "transport.bundle",
    "transport.http.stdlib_adapter",
    "transport.http.static",
)

SRC_DIRS = (
    "agent",
    "api",
    "bench",
    "evaluator",
    "ingest",
    "llm_client",
    "memory",
    "rag",
    "tests",
    "tools",
    "transport",
)


def _c(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def info(text: str) -> None:
    print(f"{_c('[..]', '36')} {text}")


def ok(text: str) -> None:
    print(f"{_c('[ok]', '32')} {text}")


def warn(text: str) -> None:
    print(f"{_c('[!!]', '33')} {text}")


def fail(text: str) -> None:
    print(f"{_c('[xx]', '31')} {text}")


def check_python() -> bool:
    current = sys.version_info[:2]
    if current < REQUIRED_PY:
        fail(f"需要 Python {REQUIRED_PY[0]}.{REQUIRED_PY[1]}+，当前是 {current[0]}.{current[1]}")
        return False
    ok(f"Python {current[0]}.{current[1]}.{sys.version_info[2]}")
    return True


def check_optional() -> dict:
    found = {}
    for name in OPTIONAL_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            found[name] = None
            continue
        version = getattr(module, "__version__", None)
        if not version:
            try:
                from importlib.metadata import version as dist_version

                version = dist_version(name)
            except Exception:  # noqa: BLE001
                version = "installed"
        found[name] = version
    missing = [name for name, version in found.items() if version is None]
    if missing:
        warn("缺少可选依赖：" + ", ".join(missing))
        warn("HTTP 层会退回标准库适配器；要完整功能执行：")
        print("        python -m pip install -r requirements-optional.txt")
    else:
        ok("可选依赖齐全：" + ", ".join(f"{n}={v}" for n, v in found.items()))
    return found


def check_config() -> bool:
    """报告本地配置来源；不打印任何密钥值。"""

    from config.loader import bootstrap, describe, status_line

    bootstrap()
    info = describe()
    ok(status_line())
    if not info["settings_exists"]:
        warn(
            "没有 config/settings.ini（可选）："
            "复制 config/settings.ini.example 后填 api_key 即可用远程模型"
        )
    return True


def check_checkpoint(path: str) -> bool:
    """启动前确认 checkpoint 没被别的进程占着（否则每个请求都会 500）。"""

    from memory.checkpoint import CheckpointLocked, ensure_writable

    try:
        info = ensure_writable(path)
    except CheckpointLocked as exc:
        fail(str(exc))
        return False
    ok(f"checkpoint 可写：{info['path']}（journal={info['journal_mode']}）")
    return True


def check_frontend() -> bool:
    index = ROOT / "web" / "index.html"
    assets = [ROOT / "web" / name for name in ("app.js", "style.css")]
    if not index.is_file():
        fail("缺少 web/index.html")
        return False
    missing = [str(path.relative_to(ROOT)) for path in assets if not path.is_file()]
    if missing:
        fail("缺少前端资源: " + ", ".join(missing))
        return False
    ok("前端资源完整：web/index.html + app.js + style.css")
    return True


def check_fixtures() -> bool:
    """方案文档要求「无样例文件时拒绝启动扮演」：没有样例就无法演示，别假装能跑。"""

    fixture = ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
    if not fixture.is_file():
        fail(
            "缺少样例聊天 fixtures/chat_laowang_xiaolin.txt；"
            "没有它无法演示扮演，请先恢复该文件（仓库内合成数据）"
        )
        return False
    ok(f"样例聊天就绪：{fixture.relative_to(ROOT)}")
    return True


def compile_sources() -> bool:
    info("编译检查（compileall）…")
    success = True
    for directory in SRC_DIRS:
        target = ROOT / directory
        if not target.is_dir():
            continue
        if not compileall.compile_dir(str(target), quiet=1, force=True):
            fail(f"编译失败：{directory}")
            success = False
    if success:
        ok("所有模块语法通过")
    return success


def smoke_imports() -> bool:
    info("导入冒烟…")
    sys.path.insert(0, str(ROOT))
    failed = []
    for name in SMOKE_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 冒烟就是要把异常都捞出来
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    if failed:
        for line in failed:
            fail(line)
        return False
    ok(f"{len(SMOKE_IMPORTS)} 个核心模块导入成功")
    return True


def run_tests(verbose: bool = False) -> bool:
    info("单元测试…")
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py"]
    if verbose:
        command.append("-v")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        fail("测试未通过")
        return False
    ok("测试全部通过")
    return True


def build(with_tests: bool = False) -> bool:
    print(_c("== 构建检查 ==", "1"))
    steps = [
        check_python,
        check_config,
        check_frontend,
        check_fixtures,
        compile_sources,
        smoke_imports,
    ]
    if not all(step() for step in steps):
        return False
    check_optional()
    if with_tests and not run_tests():
        return False
    ok("构建完成")
    return True


def _port_free(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _wait_http(host: str, port: int, timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=0.5):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    return False


def run(args) -> int:
    if not args.skip_build and not build():
        return 1

    port = args.port
    if not _port_free("127.0.0.1", port):
        fail(f"端口 {port} 已被占用，换一个：python manage.py run --port {port + 1}")
        return 1

    os.environ["PROVIDER"] = args.provider
    os.environ.setdefault("CHECKPOINT_PATH", "runtime/api.sqlite3")
    if args.stream:
        os.environ["STREAM"] = "1"
    if args.adapter:
        os.environ["ADAPTER"] = args.adapter
    if args.server:
        os.environ["SERVER"] = args.server

    if not check_checkpoint(os.environ["CHECKPOINT_PATH"]):
        return 2

    from transport.bundle import Transport

    transport = Transport(
        host="127.0.0.1",
        tcp_port=port,
        udp_port=args.udp_port,
        adapter=args.adapter or "auto",
        server=args.server or "auto",
        checkpoint_path=os.environ["CHECKPOINT_PATH"],
    )
    print(_c("== 启动服务 ==", "1"))
    info = transport.start()
    url = f"http://{info.tcp[0]}:{info.tcp[1]}/"
    print()
    print(f"  前端   {_c(url, '36')}")
    print(f"  API    {url}health · /docs（FastAPI 适配器时可用）")
    print(f"  适配器 {info.adapter} / {info.server}   模型 {transport.service.model_name}")
    print(f"  UDP    {info.udp[1] if info.udp else 'off'}   流式 {'on' if args.stream else 'off'}")
    print(f"  数据   {os.environ['CHECKPOINT_PATH']}")
    print()

    if not args.no_open:
        if _wait_http(info.tcp[0], info.tcp[1]):
            webbrowser.open(url)
            ok("已在浏览器打开（没有自动打开就手动点上面的地址）")
        else:
            warn("服务未在 20s 内响应，请检查日志")

    try:
        transport.wait()
    except KeyboardInterrupt:
        print()
        info("收到 Ctrl+C，正在停止…")
    finally:
        transport.close()
    ok("已停止")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build & run the Persona Replay demo")
    sub = parser.add_subparsers(dest="command")

    build_parser = sub.add_parser("build", help="语法检查 + 导入冒烟 + 依赖检查")
    build_parser.add_argument("--test", action="store_true", help="构建后顺便跑测试")

    test_parser = sub.add_parser("test", help="跑全部单测")
    test_parser.add_argument("-v", "--verbose", action="store_true")

    run_parser = sub.add_parser("run", help="构建检查后启动服务")
    run_parser.add_argument("--port", type=int, default=8000)
    run_parser.add_argument("--udp-port", type=int, default=None, help="观测事件 UDP 端口")
    run_parser.add_argument("--provider", choices=("auto", "heuristic", "deepseek"), default="auto")
    run_parser.add_argument("--adapter", choices=("auto", "fastapi", "stdlib"), default=None)
    run_parser.add_argument("--server", choices=("auto", "uvicorn", "granian", "stdlib"), default=None)
    run_parser.add_argument("--stream", action="store_true", help="开启 token 级流式")
    run_parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    run_parser.add_argument("--skip-build", action="store_true", help="跳过构建检查直接起")

    sub.add_parser("all", help="build + test + run")

    args = parser.parse_args(argv)
    if args.command in (None, "build"):
        if args.command is None:
            parser.print_help()
            return 0
        return 0 if build(with_tests=getattr(args, "test", False)) else 1
    if args.command == "test":
        return 0 if run_tests(verbose=args.verbose) else 1
    if args.command == "run":
        return run(args)
    if args.command == "all":
        if not build(with_tests=True):
            return 1
        namespace = argparse.Namespace(
            port=8000, udp_port=None, provider="auto", adapter=None, server=None,
            stream=False, no_open=False, skip_build=True,
        )
        return run(namespace)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
