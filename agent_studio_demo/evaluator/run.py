"""Day12：跑 10 轮固定对白，打印指标表。

用法：
    python -m evaluator.run                 # 规则评估（默认，零成本）
    python -m evaluator.run --gate          # CI 模式：不达标退出码 1
    python -m evaluator.run --judge llm     # 需要 DEEPSEEK_API_KEY
    python -m evaluator.run --json          # 机器可读
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluator.llm_judge import (
    DEFAULT_THRESHOLDS,
    FIXTURE,
    LLMJudge,
    RuleJudge,
    aggregate,
    evaluate,
    gate,
    load_rounds,
)
from llm_client.factory import create_reply_model, load_dotenv


def _print_table(records) -> None:
    header = (
        f"{'round':<22} {'group':<8} {'status':<12} {'verdict':<9} "
        f"{'problems':<26} {'evid':>4} {'style':>6} {'expect':>7}"
    )
    print(header)
    print("-" * len(header))
    for record in records:
        problems = ",".join(record.problems) or "-"
        group = "adv" if record.expect_human else "clean"
        print(
            f"{record.round_id:<22} {group:<8} {record.status:<12} {record.verdict:<9} "
            f"{problems[:26]:<26} {record.metrics.evidence_hits:>4} "
            f"{record.metrics.style_similarity:>6.3f} "
            f"{'OK' if record.expectation_ok else 'FAIL':>7}"
        )
        if record.failures:
            print(f"{'':<22} ↳ {', '.join(record.failures)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate persona replay rounds")
    parser.add_argument("--rounds", type=Path, default=None)
    parser.add_argument("--judge", choices=("rule", "llm"), default="rule")
    parser.add_argument(
        "--provider",
        choices=("heuristic", "deepseek", "auto"),
        default="heuristic",
        help="干净轮次用哪个回复模型（对抗轮次仍用脚本模型，保证可复现）",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="评估温度。默认 0：0.7 时同一轮两次结果不同，指标差异会被噪声淹没",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 means all rounds")
    parser.add_argument("--gate", action="store_true", help="exit 1 when thresholds fail")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    load_dotenv()
    rounds = load_rounds(args.rounds)
    if args.limit > 0:
        rounds = rounds[: args.limit]

    judge = RuleJudge()
    if args.judge == "llm":
        from llm_client.deepseek import chat_once

        judge = LLMJudge(call=lambda system, user: chat_once(system, user))

    try:
        default_model = create_reply_model(args.provider)
        if hasattr(default_model, "temperature"):
            default_model.temperature = args.temperature
    except (ValueError, RuntimeError) as exc:
        print(f"[provider error] {exc}", file=sys.stderr)
        return 2

    try:
        records = evaluate(
            rounds, judge, default_model=default_model, chat_path=FIXTURE
        )
    except RuntimeError as exc:
        print(f"[judge error] {exc}", file=sys.stderr)
        return 2

    metrics = aggregate(records)
    failures = gate(metrics)

    if args.json:
        print(
            json.dumps(
                {
                    "judge": judge.name,
                    "metrics": metrics,
                    "failures": failures,
                    "rounds": [record.to_dict() for record in records],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_table(records)
        print()
        print("summary:")
        for key, value in metrics.items():
            print(f"  {key:<22} {value}")
        clean_faith = metrics.get("faithfulness") or {}
        if clean_faith.get("applicable", 0) == 0:
            print(
                "  注：faithfulness 统计的是「事实断言能否被证据支持」，"
                "本轮回复全是语气句 → 不适用（不是 0 分）。"
            )
        print()
        print("thresholds:", json.dumps(DEFAULT_THRESHOLDS, ensure_ascii=False))
        if failures:
            print("gate: FAIL -> " + "; ".join(failures))
        else:
            print("gate: PASS")

    if args.gate and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
