"""Day10：检索策略 A/B。

同一份索引、同一批 query，比较四种策略的 Hit@1 / Hit@3 / MRR，以及
"不该召回时是否召回"（no_match 组的误报率）。

    python -m rag.ab_test                 # 词法 vs 词法+扩写 vs 哈希向量 vs 混合
    python -m rag.ab_test --embedder none # 只跑词法系
    python -m rag.ab_test --embedder http # 接真实 embedding 服务（需要环境变量）

诚实边界：哈希向量不是语义 embedding，它的信息量和词法相同，预期**没有提升**。
这个结论本身有价值：它说明"随便换个向量库"不会让检索变好，换的是**语义**才有用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agent.nodes import MIN_EVIDENCE_SCORE
from rag.embedding import HttpEmbedder, build_embedder
from rag.history_index import HistoryIndex
from rag.retriever import BaseRetriever, build_retrievers
from tools.chat_parser import parse_chat_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
LARGE_FIXTURE = PROJECT_ROOT / "fixtures" / "chat_multi_topic.txt"
QUERIES_FILE = PROJECT_ROOT / "fixtures" / "retrieval_eval.json"
LARGE_QUERIES_FILE = PROJECT_ROOT / "fixtures" / "retrieval_eval_large.json"


def load_queries(path: Optional[Path] = None) -> List[dict]:
    data = json.loads(Path(path or QUERIES_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("retrieval eval queries must be a non-empty list")
    return data


def score_backend(retriever: BaseRetriever, queries: Sequence[dict]) -> dict:
    ranked = {"same_situation": [], "paraphrase": []}
    false_positives = 0
    no_match_total = 0

    for item in queries:
        hits = retriever.search(item["query"], limit=5)
        expected = item.get("expected_line")
        group = item.get("group", "same_situation")

        if expected is None:
            no_match_total += 1
            if hits and hits[0].score >= MIN_EVIDENCE_SCORE:
                false_positives += 1
            continue

        rank = next(
            (position for position, hit in enumerate(hits, start=1)
             if hit.line_number == expected),
            None,
        )
        ranked.setdefault(group, []).append(rank)

    def summarise(ranks: List[Optional[int]]) -> dict:
        if not ranks:
            return {"n": 0, "hit@1": 0.0, "hit@3": 0.0, "mrr": 0.0}
        total = len(ranks)
        return {
            "n": total,
            "hit@1": round(sum(1 for r in ranks if r == 1) / total, 3),
            "hit@3": round(sum(1 for r in ranks if r and r <= 3) / total, 3),
            "mrr": round(
                sum(1.0 / r for r in ranks if r) / total, 3
            ),
        }

    by_group = {group: summarise(ranks) for group, ranks in ranked.items()}
    overall_ranks = [r for ranks in ranked.values() for r in ranks]
    overall = summarise(overall_ranks)
    overall["false_positive_rate"] = (
        round(false_positives / no_match_total, 3) if no_match_total else 0.0
    )
    return {"overall": overall, "by_group": by_group}


def print_table(results: Dict[str, dict]) -> None:
    header = (
        f"{'retriever':<34} {'n':>3} {'hit@1':>6} {'hit@3':>6} {'mrr':>6} {'fp':>5}"
    )
    print(header)
    print("-" * len(header))
    for name, result in results.items():
        overall = result["overall"]
        print(
            f"{name:<34} {overall['n']:>3} {overall['hit@1']:>6.3f} "
            f"{overall['hit@3']:>6.3f} {overall['mrr']:>6.3f} "
            f"{overall['false_positive_rate']:>5.3f}"
        )

    groups: List[str] = []
    for result in results.values():
        for group in result["by_group"]:
            if group not in groups:
                groups.append(group)
    if not groups:
        return

    print()
    print("hit@1 by group:")
    head = f"{'retriever':<34}" + "".join(f"{group:>18}" for group in groups)
    print(head)
    print("-" * len(head))
    for name, result in results.items():
        cells = []
        for group in groups:
            summary = result["by_group"].get(group)
            cells.append(
                f"{summary['hit@1']:.3f} (n={summary['n']})" if summary else "-"
            )
        print(f"{name:<34}" + "".join(f"{cell:>18}" for cell in cells))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval A/B")
    parser.add_argument(
        "--embedder",
        choices=("hashing", "http", "none"),
        default="hashing",
        help="hashing = 零依赖特征哈希；http = 真实 embedding 服务",
    )
    parser.add_argument(
        "--expander",
        choices=("none", "synonym", "llm"),
        default="synonym",
        help="查询扩写方式：手写同义词 / 大模型扩写（需要 key）",
    )
    parser.add_argument(
        "--dataset",
        choices=("small", "large"),
        default="small",
        help="small = 20 条语料的 15 条 query；large = 50 条语料的 21 条 query（含 9 条硬改写）",
    )
    parser.add_argument(
        "--rerank",
        choices=("none", "llm"),
        default="none",
        help="llm = 先词法召回 8 条，再让模型重排（语义代理，需要 key）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    fixture = LARGE_FIXTURE if args.dataset == "large" else FIXTURE
    queries_file = LARGE_QUERIES_FILE if args.dataset == "large" else QUERIES_FILE

    chat = parse_chat_file(fixture)
    index = HistoryIndex.from_chat(chat, "小林", "老王")
    queries = load_queries(queries_file)

    embedder = None
    if args.embedder == "hashing":
        embedder = build_embedder("hashing")
    elif args.embedder == "http":
        try:
            embedder = HttpEmbedder()
        except ValueError as exc:
            print(f"[skip] {exc}", file=sys.stderr)
            return 2

    expander = None
    if args.expander != "none":
        from llm_client.factory import load_dotenv
        from rag.query_expansion import build_expander

        load_dotenv()  # 扩写要用模型，必须先把 .env 读进来
        try:
            expander = build_expander(args.expander)
        except (ValueError, RuntimeError) as exc:
            print(f"[skip] expander={args.expander}: {exc}", file=sys.stderr)
            return 2

    retrievers = build_retrievers(index, embedder, expander)
    reranker = None
    if args.rerank == "llm":
        from llm_client.factory import load_dotenv
        from rag.llm_rerank import LlmReranker, RerankedRetriever

        load_dotenv()
        try:
            reranker = LlmReranker()
        except RuntimeError as exc:
            print(f"[skip] rerank=llm: {exc}", file=sys.stderr)
            return 2
        retrievers.append(RerankedRetriever(retrievers[0], reranker))

    results = {
        retriever.name: score_backend(retriever, queries) for retriever in retrievers
    }

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_table(results)
        print()
        print(
            f"dataset={args.dataset}  corpus={len(chat.messages)} 条  queries={len(queries)}"
            f"  | 误报阈值 score >= {MIN_EVIDENCE_SCORE}"
        )
        print("fp = no_match 组里被误召回的比例（越低越好）")
        if getattr(expander, "failures", 0):
            print(f"[warn] 扩写失败 {expander.failures} 次，已退回原 query")
        if getattr(reranker, "failures", 0):
            print(f"[warn] 重排失败 {reranker.failures} 次，已退回原顺序")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
