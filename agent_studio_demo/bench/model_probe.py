"""模型与回合的延迟/token 测量（方案里的 P0 埋点，可重复执行）。

    python -m bench.model_probe --provider deepseek -n 3
    python -m bench.model_probe --provider heuristic -n 3
    python -m bench.model_probe --json

测三件事：
1. **模型单次调用**：非流式耗时 + token 用量（成本）
2. **流式首 token**：TTFT（决定要不要默认开 `STREAM=1`）
3. **整轮**：`runtime.invoke` 端到端（含检索 + Critic），与上面对照就知道业务开销多少

注意：TTFT 只有远程模型才有意义；离线模型的"流式"是把整句切开，不是真流式。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import PersonaReplayRuntime
from ingest import session_store
from llm_client.factory import create_reply_model, load_dotenv
from memory.persona import ShortTermMemory, build_persona_card
from rag.history_index import HistoryIndex
from tools.chat_parser import parse_chat_file

FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
GOLDEN_QUERY = "王哥今晚加班，晚饭你自己先吃？"


def _percentile(values, ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return ordered[index]


def measure_model(provider: str, rounds: int) -> dict:
    chat = parse_chat_file(FIXTURE)
    persona = build_persona_card(chat, "小林")
    index = HistoryIndex.from_chat(chat, "小林", "老王")
    hits = index.search(GOLDEN_QUERY, limit=3)
    model = create_reply_model(provider)
    if hasattr(model, "temperature"):
        model.temperature = 0.0

    plain_ms, ttft_ms, total_ms = [], [], []
    prompt_tokens, completion_tokens = [], []
    reply_lengths = []

    for _ in range(rounds):
        started = time.perf_counter()
        reply = model.generate(
            user_input=GOLDEN_QUERY,
            persona=persona,
            retrieved=hits,
            memory=ShortTermMemory(),
        )
        plain_ms.append((time.perf_counter() - started) * 1000)
        reply_lengths.append(len(reply))
        usage = getattr(model, "last_usage", {}) or {}
        if usage.get("prompt_tokens"):
            prompt_tokens.append(usage["prompt_tokens"])
            completion_tokens.append(usage.get("completion_tokens", 0))

    for _ in range(rounds):
        started = time.perf_counter()
        list(
            model.generate_stream(
                user_input=GOLDEN_QUERY,
                persona=persona,
                retrieved=hits,
                memory=ShortTermMemory(),
            )
        )
        total_ms.append((time.perf_counter() - started) * 1000)
        ttft_ms.append(getattr(model, "last_ttft_ms", None))

    streamed_ttfts = [value for value in ttft_ms if value is not None]
    return {
        "provider": type(model).__name__,
        "non_stream_ms_p50": round(_percentile(plain_ms, 0.5), 1),
        "non_stream_ms_p95": round(_percentile(plain_ms, 0.95), 1),
        "stream_total_ms_p50": round(_percentile(total_ms, 0.5), 1),
        "ttft_ms_p50": round(_percentile(streamed_ttfts, 0.5), 1) if streamed_ttfts else None,
        "prompt_tokens_avg": round(statistics.fmean(prompt_tokens), 1) if prompt_tokens else None,
        "completion_tokens_avg": round(statistics.fmean(completion_tokens), 1)
        if completion_tokens
        else None,
        "reply_len_avg": round(statistics.fmean(reply_lengths), 1),
        "samples": rounds,
    }


def measure_turn(provider: str, rounds: int) -> dict:
    session_store.clear()
    durations = []
    for index in range(rounds):
        model = create_reply_model(provider)
        if hasattr(model, "temperature"):
            model.temperature = 0.0
        runtime = PersonaReplayRuntime(":memory:", model=model)
        try:
            started = time.perf_counter()
            runtime.invoke(
                thread_id=f"probe-{index}",
                chat_path=str(FIXTURE),
                user_speaker="老王",
                target_speaker="小林",
                user_input=GOLDEN_QUERY,
            )
            durations.append((time.perf_counter() - started) * 1000)
        finally:
            runtime.close()
    return {
        "turn_ms_p50": round(_percentile(durations, 0.5), 1),
        "turn_ms_p95": round(_percentile(durations, 0.95), 1),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure model latency / tokens / TTFT")
    parser.add_argument(
        "--provider", choices=("heuristic", "deepseek", "auto"), default="auto"
    )
    parser.add_argument("-n", "--rounds", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        result = {**measure_model(args.provider, args.rounds), **measure_turn(args.provider, args.rounds)}
    except (ValueError, RuntimeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"provider: {result['provider']}  samples={result['samples']}")
        print(f"  非流式 p50/p95 : {result['non_stream_ms_p50']} / {result['non_stream_ms_p95']} ms")
        print(f"  流式 TTFT p50  : {result['ttft_ms_p50']} ms")
        print(f"  流式总时长 p50 : {result['stream_total_ms_p50']} ms")
        print(f"  整轮 p50/p95   : {result['turn_ms_p50']} / {result['turn_ms_p95']} ms")
        print(
            f"  tokens/轮      : in={result['prompt_tokens_avg']} out={result['completion_tokens_avg']}"
        )
        print(f"  回复平均字数   : {result['reply_len_avg']}")
        if result["prompt_tokens_avg"] is None:
            print("  (该 provider 不返回 usage)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
