"""向量化后端（Day10）。

两个实现：
- `HashingEmbedder`：零依赖的**特征哈希**。它不是语义 embedding——把 bigram
  哈希进固定维度，信息量和词法检索基本等价。放在这里的价值是**证明 A/B
  harness 可跑**，并且它真的建立了一个向量索引，不是玩具。
- `HttpEmbedder`：任意 OpenAI 兼容的 `/embeddings` 接口（OpenAI、BGE、
  text-embedding-3 等）。没有 key 时不构造，A/B 会明确标"未运行"。

诚实边界：本机没有语义 embedding 服务，所以"同义改写召回率"的真实收益
无法在这里验证。harness 已就绪，接上 HTTP embedder 后一条命令出对比表。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from typing import Dict, List, Optional, Protocol, Sequence

from rag.history_index import tokenize


class Embedder(Protocol):
    name: str

    def embed(self, text: str) -> List[float]:
        ...

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        ...


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class HashingEmbedder:
    """Signed feature hashing over tokens; deterministic across processes.

    不能用内置 `hash()`：它带随机盐，同一进程内一致、跨进程不一致，
    索引没法持久化，测试也会飘。
    """

    def __init__(self, dim: int = 256):
        if dim < 16:
            raise ValueError("dim must be at least 16")
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return vector

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(text) for text in texts]


class HttpEmbedder:
    """OpenAI-compatible embeddings endpoint, with an in-process cache."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = (
            base_url or os.environ.get("EMBEDDING_BASE_URL", "")
        ).rstrip("/")
        self.api_key = (api_key or os.environ.get("EMBEDDING_API_KEY", "")).strip()
        self.model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        self.timeout = timeout
        self.name = f"http:{self.model}"
        self._cache: Dict[str, List[float]] = {}
        if not self.base_url or not self.api_key:
            raise ValueError(
                "HttpEmbedder 需要 EMBEDDING_BASE_URL 与 EMBEDDING_API_KEY"
            )

    def embed(self, text: str) -> List[float]:
        if text in self._cache:
            return self._cache[text]
        payload = json.dumps(
            {"model": self.model, "input": [text]}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        vector = [float(value) for value in body["data"][0]["embedding"]]
        self._cache[text] = vector
        return vector

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(text) for text in texts]


def build_embedder(name: str = "hashing") -> Embedder:
    if name == "hashing":
        return HashingEmbedder()
    if name == "http":
        return HttpEmbedder()
    raise ValueError(f"unknown embedder: {name!r}")
