"""Critic 的模型复核（可选）：补规则查不出的"无数字编造"。

只在这几种情况调用，避免把成本打上去：
- 规则已经判过、没有硬问题（助手腔/串台/超长/敏感都没有）；
- 回复里**有事实断言**（`evaluator.faithfulness.is_factual`）；
- 显式开启（`[features] critic_llm = 1` 或 `CRITIC_LLM=1`）。

一次调用、输出 JSON；解析失败就当作"无法判断"，不影响规则结论。
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional, Tuple

VERIFY_SYSTEM = """你是事实核查员。只判断"回复里的断言能否由证据推出"，不要评价语气。
只输出 JSON：{"supported": true/false, "reason": "一句话"}"""

VERIFY_USER = """证据（来自聊天记录，可能为空）：
{evidence}

待核查的回复：
{reply}

如果回复只是语气词/应答（如"哈哈""随便""在呀"），supported 填 true。
如果回复断言了证据里没有的事（时间、地点、人物、承诺），supported 填 false。"""


def critic_llm_enabled() -> bool:
    value = os.environ.get("CRITIC_LLM", "0").strip().lower()
    return value not in ("", "0", "false", "no")


def make_llm_check(
    call: Optional[Callable[[str, str], str]] = None,
) -> Callable[[str, str], Tuple[bool, str]]:
    """返回 `(reply, evidence) -> (supported, reason)`。"""

    if call is None:
        from llm_client.deepseek import chat_once

        call = chat_once

    def check(reply: str, evidence: str) -> Tuple[bool, str]:
        raw = call(
            VERIFY_SYSTEM,
            VERIFY_USER.format(evidence=evidence or "（无）", reply=reply),
        )
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return True, "无法解析判定，按通过处理"
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return True, "判定 JSON 非法，按通过处理"
        return bool(data.get("supported", True)), str(data.get("reason", ""))[:120]

    return check


__all__ = ["VERIFY_SYSTEM", "VERIFY_USER", "critic_llm_enabled", "make_llm_check"]
