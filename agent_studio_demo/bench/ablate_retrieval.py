"""消融：检索对最终回复到底有多大影响？

做法：同一句话、同一个模型、temperature=0，只改一件事——prompt 里有没有检索到的证据。
对比两版回复的"变化率"和"证据复用率"。

    python -m bench.ablate_retrieval
    python -m bench.ablate_retrieval --limit 10

为什么用这个指标：如果关掉检索后回复几乎不变，那检索（以及为重排做的所有工作）
对这个场景就是装饰品——这正是要验证的假设。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingest import session_store
from ingest.session_store import get_or_build
from llm_client.factory import load_dotenv
from llm_client.deepseek import DeepSeekReplyModel
from memory.persona import ShortTermMemory

FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
ROUNDS = PROJECT_ROOT / "fixtures" / "eval_rounds.json"

DEFAULT_INPUTS = [
    "王哥今晚加班，晚饭你自己先吃？",
    "那周六呢？",
    "在吗",
    "谢谢王哥",
    "晚上想吃面",
    "你最近是不是特别忙",
    "行吧",
    "周末的电影票别忘了",
    "你几点下班",
    "我先去开会了",
]


def char_overlap(reply: str, evidence: str) -> float:
    """回复里有多大比例的字符能在证据里找到（粗粒度复用指标）。"""

    if not reply:
        return 0.0
    hits = sum(1 for char in reply if char in evidence)
    return round(hits / len(reply), 3)


def judge_preference(rows: List[Dict], call) -> Dict:
    """盲评：同一条输入的两个回复，哪个更像聊天记录里的那个人。

    随机左右顺序，避免位置偏置；判官不知道哪个是"有证据"版。
    """

    import random

    rng = random.Random(42)
    votes = {"with": 0, "without": 0, "tie": 0, "judged": 0}
    details: List[Dict] = []
    for row in rows:
        if not row["changed"]:
            continue
        flip = rng.random() < 0.5
        option_a, option_b = (
            (row["reply_without"], row["reply_with"]) if flip else (row["reply_with"], row["reply_without"])
        )
        prompt = (
            f"用户说：{row['input']}\n"
            f"选项A：{option_a}\n选项B：{option_b}\n"
            '只输出 JSON：{"更像": "A" 或 "B"}'
        )
        try:
            raw = call("你在比较两句回复，哪一句更像聊天记录里的人说的。", prompt)
            start, end = raw.find("{"), raw.rfind("}")
            choice = json.loads(raw[start : end + 1]).get("更像", "A")
        except Exception:  # noqa: BLE001
            continue
        choice = str(choice).strip().upper()
        if choice not in ("A", "B"):
            continue
        with_evidence = "A" if flip else "B"
        votes["with" if choice == with_evidence else "without"] += 1
        votes["judged"] += 1
        details.append(
            {
                "input": row["input"],
                "winner": "with" if choice == with_evidence else "without",
                "with": row["reply_with"],
                "without": row["reply_without"],
            }
        )
    votes["details"] = details
    return votes


def run(limit: int, temperature: float = 0.0) -> Dict:
    load_dotenv()
    session_store.clear()
    session = get_or_build(FIXTURE, "老王", "小林")
    model = DeepSeekReplyModel(temperature=temperature)
    memory = ShortTermMemory()

    rows: List[Dict] = []
    for text in DEFAULT_INPUTS[:limit]:
        hits = [
            hit
            for hit in session.index.search(text, limit=3)
            if hit.score >= 0.12
        ]
        evidence_text = " ".join(f"{h.content} {h.context}" for h in hits)

        system = model._build_system_prompt(session.persona)
        with_evidence = model._build_user_prompt(text, hits, memory)
        without_evidence = model._build_user_prompt(text, [], memory)
        reply_with = model._chat(system=system, user=with_evidence, temperature=temperature).strip()
        reply_without = model._chat(system=system, user=without_evidence, temperature=temperature).strip()

        rows.append(
            {
                "input": text,
                "hits": len(hits),
                "reply_with": reply_with,
                "reply_without": reply_without,
                "changed": reply_with != reply_without,
                "reuse_with": char_overlap(reply_with, evidence_text),
                "reuse_without": char_overlap(reply_without, evidence_text),
            }
        )

    changed = sum(1 for row in rows if row["changed"])
    return {
        "rounds": len(rows),
        "changed": changed,
        "change_rate": round(changed / len(rows), 3) if rows else 0.0,
        "reuse_with": round(sum(r["reuse_with"] for r in rows) / len(rows), 3) if rows else 0.0,
        "reuse_without": round(sum(r["reuse_without"] for r in rows) / len(rows), 3) if rows else 0.0,
        "rows": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ablate retrieval (evidence on/off)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--judge", action="store_true", help="再用模型盲评哪版更像（多花 4-10 次调用）")
    args = parser.parse_args(argv)

    result = run(args.limit, args.temperature)
    if args.judge:
        from llm_client.deepseek import chat_once

        result["judge"] = judge_preference(result["rows"], chat_once)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"{'输入':<22} {'命中':>4} {'有证据':<16} {'无证据':<16} {'变?':>4}")
    print("-" * 70)
    for row in result["rows"]:
        print(
            f"{row['input'][:20]:<22} {row['hits']:>4} "
            f"{row['reply_with'][:14]:<16} {row['reply_without'][:14]:<16} "
            f"{'是' if row['changed'] else '否':>4}"
        )
    print()
    print(f"轮数 {result['rounds']}｜改判 {result['changed']} 轮｜变化率 {result['change_rate']:.0%}")
    print(
        f"证据字符复用率：开 {result['reuse_with']:.2%} vs 关 {result['reuse_without']:.2%}"
    )
    judge = result.get("judge")
    if judge:
        print(
            f"盲评（{judge['judged']} 轮）：有证据更像 {judge['with']} 次，"
            f"无证据更像 {judge['without']} 次"
        )
        for item in judge["details"]:
            print(f"  {item['input'][:18]:<20} 胜: {item['winner']:<8} "
                  f"有={item['with']!r} 无={item['without']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
