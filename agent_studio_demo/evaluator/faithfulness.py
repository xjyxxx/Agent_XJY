"""RAGAS 式 faithfulness：回复里的事实断言有多少能被检索证据支持。

**借鉴的是定义，不是代码**：RAGAS 的做法是把 answer 拆成原子 claim，逐条判断
能否由 context 推出，比例即 faithfulness。这里用零依赖的方式实现，原因是：
- RAGAS 本体要 datasets / embeddings / langchain 一套依赖，对我们的 4k 行项目太重；
- 我们的回复只有 2–5 个字，绝大多数根本没有事实断言——这时候最诚实的输出是
  "n/a（没有事实性断言）"，而不是硬算一个 0 或 1。

判定顺序（宁保守勿激进）：
1. 含数字 → 数字必须出现在证据里，否则算"编造"；
2. 含事实触发词（结婚/密码/地址/上次…）→ 用字符 bigram 重合度判断能否推出；
3. 其余（"哈哈""随便""在呀"）→ 不算事实断言，直接跳过。

可选的 LLM 判定：规则判"不支持"的 claim 再让模型复核一次，减少误报。
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional

# 事实触发词：出现这些词，说明这句话在断言一件具体的事，而不是情绪/应答。
FACT_WORDS = (
    "结婚", "婚礼", "生日", "地址", "密码", "电话", "手机", "名字", "公司",
    "工资", "年龄", "岁", "房子", "车", "猫", "狗", "搬家", "搬", "借", "还钱",
    "答应", "约好", "去过", "买过", "说过", "上次", "那天", "几点", "哪天",
    "多少钱", "订婚", "离婚", "怀孕", "住院", "出差", "请假",
)

_SPLIT_RE = re.compile(r"[。！？；;!?\n]+|，|,")
# 数字：阿拉伯数字，或"中文数字 + 量词/时间单位"。
# 后者必须带上下文，否则"一起"里的"一"会被当成数字，把语气句误判成事实句。
_NUMBER_RE = re.compile(
    r"\d+|[一二三四五六七八九十百千万零两]+(?=[点号岁块元年月日个次天周分秒])"
)
_WS_RE = re.compile(r"\s+")

SUPPORT_THRESHOLD = 0.34


def _normalize(text: str) -> str:
    return _WS_RE.sub("", text or "").lower()


def _bigrams(text: str) -> set:
    clean = _normalize(text)
    return {clean[index : index + 2] for index in range(len(clean) - 1)}


def split_claims(reply: str) -> List[str]:
    """按标点切句；太短的语气词不单独成句。"""

    parts = [part.strip() for part in _SPLIT_RE.split(reply or "")]
    return [part for part in parts if part]


def is_factual(claim: str) -> bool:
    if _NUMBER_RE.search(claim):
        return True
    return any(word in claim for word in FACT_WORDS)


def support_native(claim: str, evidence: Iterable[str]) -> tuple[bool, str]:
    """不调模型的判定：数字必须出现；其余看 bigram 重合度。"""

    corpus = " ".join(evidence or [])
    if not corpus.strip():
        return False, "没有证据"

    numbers = _NUMBER_RE.findall(claim)
    if numbers:
        missing = [number for number in numbers if number not in corpus]
        if missing:
            return False, f"数字不在证据里：{','.join(missing)}"
        return True, "数字可在证据中找到"

    claim_grams = _bigrams(claim)
    if not claim_grams:
        return False, "无法比对（claim 太短）"
    evidence_grams = _bigrams(corpus)
    overlap = len(claim_grams & evidence_grams) / len(claim_grams)
    if overlap >= SUPPORT_THRESHOLD:
        return True, f"与证据重合 {overlap:.2f}"
    return False, f"与证据重合过低 {overlap:.2f}"


def score(
    reply: str,
    evidence: Iterable[str],
    judge: Optional[Callable[[str, str], bool]] = None,
) -> Dict:
    """返回 faithfulness 及其明细。

    `faithfulness` 为 None 表示"没有事实断言"——这不是 0 分，而是"不适用"。
    """

    evidence_list = [item for item in (evidence or []) if item]
    claims = split_claims(reply)
    factual = [claim for claim in claims if is_factual(claim)]
    if not factual:
        return {
            "faithfulness": None,
            "reason": "no_factual_claims",
            "claims": len(claims),
            "factual": 0,
            "supported": 0,
            "unsupported": [],
        }

    supported = 0
    unsupported: List[Dict[str, str]] = []
    for claim in factual:
        ok, reason = support_native(claim, evidence_list)
        if not ok and judge is not None:
            try:
                ok = bool(judge(claim, "\n".join(evidence_list)))
                reason = "模型判定可支持" if ok else reason
            except Exception:  # noqa: BLE001 - 判定失败就用规则结果
                pass
        if ok:
            supported += 1
        else:
            unsupported.append({"claim": claim, "reason": reason})

    return {
        "faithfulness": round(supported / len(factual), 3),
        "reason": "ok",
        "claims": len(claims),
        "factual": len(factual),
        "supported": supported,
        "unsupported": unsupported,
    }


def aggregate(scores: Iterable[Optional[Dict]]) -> Dict:
    """把多轮的 faithfulness 汇总；n/a 的轮次单独计数，不进分母。"""

    applicable = [item for item in scores if item and item.get("faithfulness") is not None]
    skipped = sum(1 for item in scores if not item or item.get("faithfulness") is None)
    if not applicable:
        return {"faithfulness": None, "rounds": skipped, "applicable": 0, "skipped": skipped}
    mean = sum(item["faithfulness"] for item in applicable) / len(applicable)
    return {
        "faithfulness": round(mean, 3),
        "rounds": len(applicable) + skipped,
        "applicable": len(applicable),
        "skipped": skipped,
    }


__all__ = [
    "FACT_WORDS",
    "SUPPORT_THRESHOLD",
    "aggregate",
    "is_factual",
    "score",
    "split_claims",
    "support_native",
]
