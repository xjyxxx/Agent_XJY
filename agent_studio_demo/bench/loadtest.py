"""基准压测（`python -m bench.loadtest`）。

同一套 handler，三个 server 后端，各跑两段：

    A. GET /health          —— 纯传输开销（不含业务）
    B. POST /messages       —— 端到端一轮扮演（含检索/生成/Critic）

用 keep-alive 连接 + 固定线程数，测的是"在这台机器上哪个更快"，
不是"哪个在论文里更快"。跑分脚本必须能被质疑，所以参数、线程数、时长全部显式。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from transport.bundle import _free_port, wait_for_port


FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"
SERVER_STARTUP_TIMEOUT = 30.0


def _percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return ordered[index]


class Connection:
    """一个线程一条 keep-alive 连接。"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._conn = http.client.HTTPConnection(host, port, timeout=10)

    def request(self, method: str, path: str, payload=None) -> Tuple[int, bytes]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            self._conn.request(method, path, body=body, headers=headers)
            response = self._conn.getresponse()
            data = response.read()
            return response.status, data
        except (http.client.HTTPException, OSError):
            self._conn.close()
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
            raise

    def close(self) -> None:
        self._conn.close()


def _run_workers(
    host: str,
    port: int,
    workers: int,
    duration: float,
    make_job,
) -> Dict[str, float]:
    latencies: List[float] = []
    errors = [0]
    lock = threading.Lock()
    deadline = time.time() + duration

    def worker(index: int) -> None:
        connection = Connection(host, port)
        local: List[float] = []
        try:
            job = make_job(connection, index)
            for action in job:
                if time.time() >= deadline:
                    break
                started = time.perf_counter()
                try:
                    action()
                    local.append((time.perf_counter() - started) * 1000)
                except Exception:
                    with lock:
                        errors[0] += 1
                    break
        finally:
            connection.close()
            with lock:
                latencies.extend(local)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    count = len(latencies)
    return {
        "count": count,
        "rps": round(count / elapsed, 1) if elapsed else 0.0,
        "p50_ms": round(_percentile(latencies, 0.50), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "p99_ms": round(_percentile(latencies, 0.99), 2),
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        "errors": errors[0],
    }


def _health_job(connection: Connection, _index: int):
    def loop():
        while True:
            yield lambda: connection.request("GET", "/health")

    return loop()


def _turn_job(connection: Connection, index: int):
    payload = {
        "path": FIXTURE,
        "user_speaker": "老王",
        "target_speaker": "小林",
        "session_id": f"bench-{index}-{int(time.time())}",
    }
    status, body = connection.request("POST", "/sessions", payload)
    if status != 201:
        raise RuntimeError(f"session creation failed: {status} {body[:200]!r}")
    session_id = json.loads(body)["session_id"]

    def loop():
        while True:
            yield lambda: connection.request(
                "POST",
                f"/sessions/{session_id}/messages",
                {"message": GOLDEN_QUERY},
            )

    return loop()


def _kill_tree(process) -> None:
    """结束服务器进程（Windows 上要连子进程一起杀，否则 granian 会变孤儿）。"""

    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
        )
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            process.kill()
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            process.kill()


def bench_backend(server: str, adapter: str, duration: float, health_workers: int,
                  turn_workers: int) -> dict:
    """服务器跑在独立进程里，压测在父进程 —— 否则 GIL 会把服务器算慢。"""

    port = _free_port()
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bench.serve",
            "--adapter",
            adapter,
            "--server",
            server,
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not wait_for_port("127.0.0.1", port, SERVER_STARTUP_TIMEOUT):
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read() or b""
            raise RuntimeError(
                f"server did not start: {stderr.decode('utf-8', 'replace')[:400]}"
            )

        host = "127.0.0.1"
        # 预热：让服务器、线程池、模型都就位
        warm = Connection(host, port)
        for _ in range(20):
            warm.request("GET", "/health")
        warm.close()

        health = _run_workers(host, port, health_workers, duration, _health_job)
        turns = _run_workers(host, port, turn_workers, duration, _turn_job)
        return {
            "adapter": adapter,
            "server": server,
            "health": health,
            "turns": turns,
        }
    finally:
        _kill_tree(process)


def print_report(results: List[dict]) -> None:
    header = (
        f"{'server':<10} {'adapter':<9} "
        f"{'health rps':>11} {'p50':>7} {'p95':>7} "
        f"{'turns/s':>9} {'p50':>7} {'p95':>7} {'err':>4}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        health = result["health"]
        turns = result["turns"]
        print(
            f"{result['server']:<10} {result['adapter']:<9} "
            f"{health['rps']:>11.1f} {health['p50_ms']:>7.2f} {health['p95_ms']:>7.2f} "
            f"{turns['rps']:>9.2f} {turns['p50_ms']:>7.1f} {turns['p95_ms']:>7.1f} "
            f"{health['errors'] + turns['errors']:>4}"
        )
    print()
    print("health = GET /health（纯传输）；turns/s = 端到端一轮扮演（含检索/生成/Critic）")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Load-test the transport backends")
    parser.add_argument("--servers", default="stdlib,uvicorn,granian")
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--health-workers", type=int, default=8)
    parser.add_argument("--turn-workers", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    results = []
    for name in [item.strip() for item in args.servers.split(",") if item.strip()]:
        adapter = "stdlib" if name == "stdlib" else "fastapi"
        try:
            result = bench_backend(
                name, adapter, args.duration, args.health_workers, args.turn_workers
            )
        except Exception as exc:  # noqa: BLE001 - 跑分脚本要能跳过不可用后端
            print(f"[skip] {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        results.append(result)

    if not results:
        print("no backend available", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"duration={args.duration}s  health_workers={args.health_workers}  "
              f"turn_workers={args.turn_workers}")
        print()
        print_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
