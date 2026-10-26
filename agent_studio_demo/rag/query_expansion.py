"""查询扩写：把"换个说法"的 query 拉回字面可检索的形态。

两条路：
- `SynonymExpander`：手写领域同义词表，零成本、零延迟，但只覆盖写进去的词。
- `LlmExpander`：让模型把 query 扩成"同一情境下聊天里可能出现的关键词"。
  一次调用、结果按 query 缓存，失败自动退回原 query（检索不能因为扩写挂了）。

为什么不用 embedding：DeepSeek 没有 embeddings 接口，而用户明确不下载本地模型。
扩写是"用现有模型补语义"的折中：它把语义对齐的活儿从向量空间挪到了生成侧。
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from rag.retriever import DEFAULT_SYNONYMS, expand_query


EXPANSION_SYSTEM = """你在帮一个聊天记录检索系统做查询扩展。只输出 JSON 数组，不要解释。
任务：把用户这句话，扩写成 3-5 个"在类似聊天里可能出现的中文关键词"（名词/动词为主），
用于字面检索。不要重复原句，不要输出整句，不要输出标点。"""

EXPANSION_USER = """用户这句话：{query}
聊天里的人设：{persona}

输出形如 ["关键词1","关键词2"]。
"""


class SynonymExpander:
    name = "synonym"

    def __init__(self, synonyms: Optional[Dict[str, tuple]] = None):
        self.synonyms = synonyms or DEFAULT_SYNONYMS

    def expand(self, query: str) -> str:
        return expand_query(query, self.synonyms)

    def variants(self, query: str) -> List[str]:
        expanded = self.expand(query)
        return [query] if expanded == query else [query, expanded]


class LlmExpander:
    """LLM 扩写，带进程内缓存与失败兜底。"""

    name = "llm"

    def __init__(
        self,
        call: Callable[[str, str], str],
        persona: str = "",
        max_keywords: int = 5,
        cache: Optional[Dict[str, str]] = None,
    ):
        self._call = call
        self.persona = persona
        self.max_keywords = max_keywords
        self.cache: Dict[str, str] = cache if cache is not None else {}
        self.failures = 0

    def keywords(self, query: str) -> List[str]:
        if query in self.cache:
            return json.loads(self.cache[query])
        try:
            raw = self._call(
                EXPANSION_SYSTEM,
                EXPANSION_USER.format(query=query, persona=self.persona or "普通朋友聊天"),
            )
            keywords = self._parse(raw)
        except Exception:  # noqa: BLE001 - 扩写失败不能影响检索
            self.failures += 1
            return []
        self.cache[query] = json.dumps(keywords, ensure_ascii=False)
        return keywords

    def expand(self, query: str) -> str:
        keywords = self.keywords(query)
        return f"{query} {' '.join(keywords)}" if keywords else query

    def variants(self, query: str) -> List[str]:
        """每一路单独检索，交给融合层按名次合并——而不是把关键词拼进 query。

        拼进去会让 query 变长、余弦分母变大，把原本匹配的那条稀释掉（实测过）。
        """

        return [query, *self.keywords(query)]

    def _parse(self, raw: str) -> List[str]:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            items = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []
        keywords = [str(item).strip() for item in items if str(item).strip()]
        # 去掉和原句完全重复的、超长的
        return [item for item in keywords if len(item) <= 8][: self.max_keywords]


def build_expander(name: str = "synonym", **kwargs):
    if name in ("synonym", "none"):
        return SynonymExpander()
    if name == "llm":
        from llm_client.deepseek import chat_once

        return LlmExpander(call=kwargs.pop("call", chat_once), **kwargs)
    raise ValueError(f"unknown expander: {name!r} (syonym|llm)")
