"""方案文档「场景验收标准（7 条）」的可执行验收。

    python -m tests.acceptance                  # 用当前 .env 的模型（有 key 就是 DeepSeek）
    python -m tests.acceptance --provider heuristic
    python -m tests.acceptance --json

每条打印 PASS/FAIL + 证据，最后给总表。任何一条 FAIL 退出码为 1。
不是单元测试：这里跑的是"这个产品形态成不成立"。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.service import SessionService
from ingest import session_store
from llm_client.factory import create_reply_model, load_dotenv
from llm_client.scripted import ScriptedModel
from transport.events import EventBus, NullSink
from transport.handlers import ApiHandlers
from transport.http.stdlib_adapter import StdlibHttpServer

FIXTURE = str(PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt")
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"

results: list[dict] = []


def record(no: int, name: str, passed: bool, evidence: str) -> None:
    results.append({"no": no, "name": name, "pass": passed, "evidence": evidence})
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {no}. {name}\n        {evidence}")


def _service(provider: str, model=None) -> SessionService:
    session_store.clear()
    if model is None:
        model = create_reply_model(provider)
        if hasattr(model, "temperature"):
            model.temperature = 0.0
    return SessionService(":memory:", model=model)


@contextmanager
def _http(service: SessionService):
    server = StdlibHttpServer(ApiHandlers(service, EventBus([NullSink()])), port=0).start()
    try:
        yield f"http://{server.host}:{server.port}"
    finally:
        server.shutdown()


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read().decode("utf-8")


def check_1_import(provider: str) -> None:
    service = _service(provider)
    try:
        with _http(service) as base:
            data = _post(f"{base}/chats/import", {"path": FIXTURE})
        ok = (
            data["speakers"] == ["老王", "小林"]
            and data["message_count"] == 20
            and data["persona"]["speaker"] == "小林"
        )
        record(1, "导入：解析、列说话人、建人设卡", ok,
               f"speakers={data['speakers']} messages={data['message_count']} "
               f"persona={data['persona']['speaker']} avg_len={data['persona']['average_message_length']}")
    finally:
        service.close()


def check_2_main_path(provider: str) -> None:
    service = _service(provider)
    try:
        session = service.create_session(FIXTURE, "老王", "小林")
        turn = service.send(session.thread_id, GOLDEN_QUERY)
        critique = turn.critique or {}
        ok = bool(turn.reply) and critique.get("verdict") == "pass"
        record(2, "主路径：检索后按对方语气回复", ok,
               f"user={GOLDEN_QUERY!r} reply={turn.reply!r} "
               f"verdict={critique.get('verdict')} model={service.model_name}")
    finally:
        service.close()


def check_3_evidence(provider: str) -> None:
    service = _service(provider)
    try:
        with _http(service) as base:
            session = _post(f"{base}/sessions", {
                "path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"
            })
            body = _get(
                f"{base}/sessions/{session['session_id']}/stream"
                f"?message={urllib.request.quote(GOLDEN_QUERY)}"
            )
        events = [line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")]
        retrieved = [
            json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ") and '"hits"' in line
        ]
        hits = retrieved[0]["hits"] if retrieved else []
        ok = bool(hits) and "retrieved" in events and hits[0].get("line_number")
        record(3, "依据可见：SSE 能打出召回原句", ok,
               f"order={events[:6]}… top1={hits[0]['content']!r} "
               f"line={hits[0]['line_number']} score={hits[0]['score']}" if hits else "无召回")
    finally:
        service.close()


def check_4_tone(provider: str) -> None:
    cases = [
        ("assistant_tone", "作为AI，我建议你早点休息"),
        ("crosstalk", "另外，周末的电影票我已经买好了，别临时放我鸽子"),
    ]
    details = []
    passed = True
    for expected, reply in cases:
        session_store.clear()
        service = SessionService(":memory:", model=ScriptedModel([reply]))
        try:
            session = service.create_session(FIXTURE, "老王", "小林")
            turn = service.send(session.thread_id, GOLDEN_QUERY)
            problems = (turn.critique or {}).get("problems", [])
            hit = expected in problems and turn.status == "interrupted"
            passed = passed and hit
            details.append(f"{expected}={'拦下' if hit else '漏过'}")
        finally:
            service.close()
    record(4, "语气约束：Critic 拦助手腔与串台", passed, "；".join(details))


def check_5_hallucination(provider: str) -> None:
    probes = ["我们什么时候结的婚", "你银行卡密码是多少", "我们不是约好去三亚吗"]
    details = []
    passed = True
    service = _service(provider)
    try:
        for index, probe in enumerate(probes):
            session = service.create_session(FIXTURE, "老王", "小林")
            turn = service.send(session.thread_id, probe)
            reply = turn.reply or ""
            leaks = bool(re.search(r"\d{2,4}|密码是|结婚于|三亚", reply))
            meta = any(word in reply for word in ("记录", "资料", "上下文"))
            ok = not leaks and not meta and (turn.status in {"ok", "interrupted", "rejected"})
            passed = passed and ok
            details.append(f"{probe}→{reply!r}{'' if ok else '（可疑）'}")
            # 若被判敏感进入 HITL，先拒绝掉，避免影响后续
            if turn.status == "interrupted":
                service.resume(session.thread_id, {"action": "reject"})
    finally:
        service.close()
    record(5, "反幻觉：记录里没有的事不编造", passed, "；".join(details))


def check_6_memory(provider: str) -> None:
    service = _service(provider)
    try:
        session = service.create_session(FIXTURE, "老王", "小林")
        last = None
        for index in range(8):
            message = GOLDEN_QUERY if index == 0 else f"第{index}轮：那周六呢？"
            last = service.send(session.thread_id, message)
        reloaded = service.get(session.thread_id)
        ok = (
            last.turn_count == 8
            and len(last.short_memory) == 6
            and bool(last.memory_summary.strip())
            and reloaded.turn_count == 8
        )
        record(6, "记忆：多轮不丢主题 + 同 thread 续跑", ok,
               f"turn_count={last.turn_count} window={len(last.short_memory)} "
               f"summary_len={len(last.memory_summary)} 重新加载 turn_count={reloaded.turn_count}")
    finally:
        service.close()


def check_7_service(provider: str) -> None:
    service = _service(provider)
    try:
        with _http(service) as base:
            session = _post(f"{base}/sessions", {
                "path": FIXTURE, "user_speaker": "老王", "target_speaker": "小林"
            })
            turn = _post(f"{base}/sessions/{session['session_id']}/messages",
                         {"message": GOLDEN_QUERY})
            stream = _get(f"{base}/sessions/{session['session_id']}/stream"
                          f"?message={urllib.request.quote('那周六呢？')}")
        events = [line[len("event: "):] for line in stream.splitlines() if line.startswith("event: ")]
        ok = (
            turn["reply"] and turn["status"] == "ok"
            and events[0] == "persona" and events[-1] == "done"
            and "retrieved" in events
        )
        record(7, "服务：import / sessions / stream 演示完整一轮", ok,
               f"turn.reply={turn['reply']!r} sse={events[:5]}…done")
    finally:
        service.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the 7 acceptance checks")
    parser.add_argument("--provider", choices=("auto", "heuristic", "deepseek"), default="auto")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    print(f"provider={args.provider}\n")
    try:
        check_1_import(args.provider)
        check_2_main_path(args.provider)
        check_3_evidence(args.provider)
        check_4_tone(args.provider)
        check_5_hallucination(args.provider)
        check_6_memory(args.provider)
        check_7_service(args.provider)
    except Exception as exc:  # noqa: BLE001 - 验收脚本要报清楚哪条炸了
        print(f"\n[ABORT] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    passed = sum(1 for item in results if item["pass"])
    print(f"\n{passed}/{len(results)} 条通过")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
