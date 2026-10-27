"""可替换的检索引擎（Day10）。

同一份 `HistoryIndex` 上挂不同策略，用同一批 query 做 A/B：

- `LexicalRetriever`：词法（bigram + 上下文加权），当前主链路用的就是这个。
- `ExpandedRetriever`：词法 + 查询扩写（一个很小的领域同义词表）。
- `VectorRetriever`：向量余弦，权重与词法一致（上下文 0.7 / 回复 0.3），保证可比。
- `HybridRetriever`：多个检索器用 RRF 融合。

为什么融合用 RRF 而不是加权求和：余弦在 0–1、词法分数也在 0–1 但分布完全不同，
权重得针对数据集调，换个数据集就废。RRF 只看名次，零调参。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from rag.embedding import Embedder, cosine
from rag.history_index import CONTENT_WEIGHT, CONTEXT_WEIGHT, HistoryHit, HistoryIndex


# 领域同义词表：只覆盖样例里真实出现的说法。
# 这是"查询扩写"，不是"语义理解"——它修饰不了没写进表里的说法。
# 注意：它能让 hard 组从 0 变成 1，但那是**人工先验**的功劳，不是向量库的功劳。
DEFAULT_SYNONYMS = {
    "晚饭": ("吃饭", "面"),
    "吃饭": ("晚饭", "面"),
    "面馆": ("面", "吃饭"),
    "周六": ("周末", "周六"),
    "电影票": ("电影",),
    "片子": ("电影", "电影票"),
    "加班": ("晚点", "忙"),
    "晚点": ("加班",),
    "称呼": ("王哥", "叫"),
}


def expand_query(query: str, synonyms: Optional[Dict[str, Sequence[str]]] = None) -> str:
    """Append domain synonyms to the query text (cheap query expansion)."""

    table = synonyms or DEFAULT_SYNONYMS
    extras: List[str] = []
    for trigger, expansions in table.items():
        if trigger in query:
            extras.extend(expansions)
    if not extras:
        return query
    return query + " " + " ".join(dict.fromkeys(extras))


class BaseRetriever:
    name = "base"

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        raise NotImplementedError


class LexicalRetriever(BaseRetriever):
    name = "lexical"

    def __init__(self, index: HistoryIndex):
        self.index = index

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        return self.index.search(query, limit=limit)


class ExpandedRetriever(BaseRetriever):
    """词法 + 查询扩写。扩写器可换：手写同义词 / LLM / 任何有 `.expand()` 的对象。"""

    name = "lexical+expand"

    def __init__(self, index: HistoryIndex, expander=None):
        self.index = index
        self.expander = expander
        if expander is not None:
            self.name = "lexical+" + getattr(expander, "name", "expand")

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        expanded = (
            self.expander.expand(query) if self.expander is not None else expand_query(query)
        )
        return self.index.search(expanded, limit=limit)


class VectorRetriever(BaseRetriever):
    name = "vector"

    def __init__(self, index: HistoryIndex, embedder: Embedder):
        self.embedder = embedder
        self.name = f"vector:{embedder.name}"
        self.entries = list(index.entries)
        self._context_vectors = embedder.embed_many(
            [entry.context for entry in self.entries]
        )
        self._content_vectors = embedder.embed_many(
            [entry.content for entry in self.entries]
        )

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        query_vector = self.embedder.embed(query)
        hits: List[HistoryHit] = []
        for position, entry in enumerate(self.entries):
            context_score = cosine(query_vector, self._context_vectors[position])
            content_score = cosine(query_vector, self._content_vectors[position])
            score = CONTEXT_WEIGHT * context_score + CONTENT_WEIGHT * content_score
            hits.append(
                HistoryHit(
                    content=entry.content,
                    context=entry.context,
                    speaker=entry.speaker,
                    line_number=entry.line_number,
                    score=round(min(score, 0.9999), 4),
                    context_score=round(context_score, 4),
                    content_score=round(content_score, 4),
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.line_number))
        return hits[:limit]


class HybridRetriever(BaseRetriever):
    """Reciprocal Rank Fusion over several retrievers."""

    name = "hybrid"

    def __init__(self, retrievers: Sequence[BaseRetriever], rrf_k: int = 60):
        if not retrievers:
            raise ValueError("hybrid retriever needs at least one retriever")
        self.retrievers = list(retrievers)
        self.rrf_k = rrf_k
        self.name = "hybrid(" + "+".join(r.name for r in self.retrievers) + ")"

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        fused: Dict[Tuple[str, int], float] = {}
        by_key: Dict[Tuple[str, int], HistoryHit] = {}

        for retriever in self.retrievers:
            for rank, hit in enumerate(retriever.search(query, limit=limit), start=1):
                key = (hit.speaker, hit.line_number)
                fused[key] = fused.get(key, 0.0) + 1.0 / (self.rrf_k + rank)
                by_key.setdefault(key, hit)

        ranked = sorted(fused.items(), key=lambda item: -item[1])[:limit]
        results: List[HistoryHit] = []
        for key, score in ranked:
            base = by_key[key]
            results.append(
                HistoryHit(
                    content=base.content,
                    context=base.context,
                    speaker=base.speaker,
                    line_number=base.line_number,
                    score=round(score, 6),
                    context_score=base.context_score,
                    content_score=base.content_score,
                )
            )
        return results


class MultiQueryRetriever(BaseRetriever):
    """多路检索 + RRF（**默认不启用**）。

    实测结论（`fixtures/retrieval_eval_large.json`，hard 组 9 条）：
    关键词单独检索最多把 2 条从 rank2→1 / rank5→4，另有 4 条**根本召回不到**；
    等权 RRF 融合会让短关键词命中淹没原句信号，hit@1 从 0.333 掉到 0.222。
    所以这里只保留实现作为对照，`build_retrievers` 不挂它。

    真正的瓶颈是"语料侧没有语义信号"（问"假期想去海边" vs 上文"请三天假够玩"），
    要靠 embedding 解决，不是靠融合。
    """

    name = "lexical+multiquery"

    def __init__(self, index: HistoryIndex, expander, rrf_k: int = 60):
        self.index = index
        self.expander = expander
        self.rrf_k = rrf_k
        self.name = "lexical+multiquery+" + getattr(expander, "name", "expand")

    def search(self, query: str, limit: int = 5) -> List[HistoryHit]:
        variants = (
            self.expander.variants(query)
            if hasattr(self.expander, "variants")
            else [query, self.expander.expand(query)]
        )
        fused: Dict[Tuple[str, int], float] = {}
        by_key: Dict[Tuple[str, int], HistoryHit] = {}
        for variant in dict.fromkeys(variants):
            for rank, hit in enumerate(
                self.index.search(variant, limit=max(limit * 2, 4)), start=1
            ):
                key = (hit.speaker, hit.line_number)
                fused[key] = fused.get(key, 0.0) + 1.0 / (self.rrf_k + rank)
                by_key.setdefault(key, hit)
        ranked = sorted(fused.items(), key=lambda item: -item[1])[:limit]
        return [
            HistoryHit(
                content=by_key[key].content,
                context=by_key[key].context,
                speaker=by_key[key].speaker,
                line_number=by_key[key].line_number,
                score=round(by_key[key].score, 4),
                context_score=by_key[key].context_score,
                content_score=by_key[key].content_score,
            )
            for key, _ in ranked
        ]


def build_retrievers(
    index: HistoryIndex,
    embedder: Optional[Embedder] = None,
    expander=None,
) -> List[BaseRetriever]:
    """The fixed A/B set (kept stable so numbers stay comparable)."""

    synonyms = ExpandedRetriever(index)
    retrievers: List[BaseRetriever] = [LexicalRetriever(index), synonyms]
    best_lexical: BaseRetriever = synonyms
    if expander is not None:
        best_lexical = ExpandedRetriever(index, expander)
        retrievers.append(best_lexical)
        # MultiQueryRetriever 保留实现但默认不挂：实测融合会掉点，见类注释
    if embedder is not None:
        vector = VectorRetriever(index, embedder)
        retrievers.append(vector)
        retrievers.append(HybridRetriever([best_lexical, vector]))
    return retrievers
