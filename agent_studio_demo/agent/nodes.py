"""Day8-Day11：Planner / Worker / Critic / HumanReview 节点。

节点约定（本项目的编排契约）：
1. 每个节点是 `(state) -> dict` 的纯函数式偏更新，返回**增量**，不原地改 state。
2. 节点不做条件判断分叉，分叉由 `route_after_critic` 之类的 router 决定，
   这样图结构可以被画出来、被测试。
3. 失败分三种：
   - 代码 bug → 抛 ValueError / RuntimeError。
   - 模型不听话（太长、助手腔、编数字）→ 走 rewrite 回边，最多两次。
   - 需要人拍板（敏感内容 / 重写预算用尽）→ 抛 GraphInterrupt，暂停等人。
"""

from __future__ import annotations

import os
import re
from typing import List, Sequence

from agent.control import GraphInterrupt
from ingest.session_store import ChatSession, get_or_build, get_session
from llm_client.heuristic import ReplyModel
from memory.persona import PersonaCard, ShortTermMemory
from rag.history_index import HistoryHit


# 明显的助手腔特征。这些用正则判，成本为 0，不需要调模型。
ASSISTANT_TONE = (
    "作为ai",
    "作为 ai",
    "作为一个",
    "以下是",
    "建议你",
    "我可以帮你",
    "希望对你",
    "总结一下",
    "很高兴为您",
    "首先，",
)

# 敏感类目：命中也用规则判，命中即交人，不做自动重写。
# 理由：语气问题重写能修，"要不要答应借钱"重写修不了，那是人的决定。
SENSITIVE_PATTERNS = {
    "credential": ("密码", "验证码", "身份证", "银行卡", "账号"),
    "money": ("转账", "打钱", "借钱", "借我", "借你", "红包", "工资", "多少钱"),
    "address": ("家庭住址", "住哪", "门牌", "具体地址"),
    "commitment": ("保证", "答应你", "发誓", "一定会", "结婚", "领证"),
    "health": ("吃药", "病历", "确诊", "医院"),
}

# 人工拒绝时的兜底回复：短、中性、不承诺任何事。
DEGRADED_REPLY = "嗯，知道了"

_NUMBER = re.compile(r"\d+")

# 串台判定：回复里出现另一人原话的连续 6 个字，视为抄了别人的口吻。
CROSSTALK_WINDOW = 6

# 证据分数低于这个值就不算"依据"。宁可返回空证据（明确说"记录里没提过"），
# 也不要拿 0.0009 分的噪声去凑 top-3 —— 那会让"依据可见"变成"依据可疑"。
#
# 0.12 是量出来的，不是拍的：Day12 跑 10 条 query 看原始分布，噪声集中在
# 0.0009–0.10（"谢谢王哥" 0.1003、"我们什么时候结的婚" 0.0607），真正相关的
# 从 0.165 起（"晚上想吃面" 0.1651、"电影票" 0.1851），黄金样例 0.5274。
MIN_EVIDENCE_SCORE = 0.12


# 人工裁决只接受这三个动作；两个引擎（自研 / 官方 LangGraph）共用这份定义。
VALID_ACTIONS = ("approve", "edit", "reject")


def _require_session(state: dict) -> tuple[ChatSession, dict]:
    """Get the imported session, rebuilding from chat_path when needed."""

    session = get_session(str(state.get("chat_id") or ""))
    if session is not None:
        return session, {}

    chat_path = state.get("chat_path")
    if not chat_path:
        raise ValueError(
            "no chat session in state: run planner_node first, or pass chat_path"
        )
    session = get_or_build(
        chat_path,
        state.get("user_speaker") or None,
        state.get("target_speaker") or None,
    )
    return session, {
        "chat_id": session.chat_id,
        "user_speaker": session.user_speaker,
        "target_speaker": session.target_speaker,
        "speakers": session.speakers,
        "persona": session.persona.to_dict(),
        "persona_ready": True,
    }


# --------------------------------------------------------------------------
# Planner：导入时做一次，产出人设卡与索引；之后的轮次直接跳过。
# --------------------------------------------------------------------------
def planner_node(state: dict) -> dict:
    if state.get("persona_ready") and state.get("persona"):
        return {}

    chat_path = state.get("chat_path")
    if not chat_path:
        raise ValueError("chat_path is required for the first turn")

    session = get_or_build(
        chat_path,
        state.get("user_speaker") or None,
        state.get("target_speaker") or None,
    )
    return {
        "chat_id": session.chat_id,
        "user_speaker": session.user_speaker,
        "target_speaker": session.target_speaker,
        "speakers": session.speakers,
        "persona": session.persona.to_dict(),
        "persona_ready": True,
    }


# --------------------------------------------------------------------------
# Worker 前半：检索证据（Day10 的问答对召回在这里落地）。
# --------------------------------------------------------------------------
def retrieve_node(
    state: dict,
    limit: int = 3,
    min_score: float = MIN_EVIDENCE_SCORE,
) -> dict:
    session, patch = _require_session(state)
    query = str(state.get("user_input", ""))
    # 开了重排就先深召回（8 条），否则只取 limit 条——深召回会让重排有东西可排。
    depth = max(8, limit) if _rerank_enabled() else limit
    hits = session.index.search(query, limit=depth)
    if _rerank_enabled():
        hits = _rerank(query, hits, limit=limit)
    return {
        **patch,
        "retrieved": [hit.to_dict() for hit in hits if hit.score >= min_score],
    }


# 重排器按进程缓存：每次新建会重复导入/读配置，但也没有状态需要共享。
_RERANKER = None


def _rerank_enabled() -> bool:
    return os.environ.get("RERANK_LLM", "0").strip().lower() not in ("", "0", "false", "no")


def _rerank(query: str, hits, limit: int):
    """可选的语义重排（默认关闭）。

    每轮多一次模型调用（约 +0.5s）。基准里 hard 组 hit@1 从 0.333 → 0.556，
    但 n=9、McNemar p≈0.5，**未达统计显著**——所以默认不开，只作为可选项。
    """

    global _RERANKER
    if _RERANKER is None:
        try:
            from rag.llm_rerank import LlmReranker

            _RERANKER = LlmReranker(depth=max(8, limit))
        except Exception:  # noqa: BLE001 - 没 key 就退回纯词法
            _RERANKER = False
    if not _RERANKER:
        return hits
    return _RERANKER.rerank(query, list(hits))[:limit]


# --------------------------------------------------------------------------
# Worker 后半：按人设与证据起草；重写时带上上一轮的 critique。
# --------------------------------------------------------------------------
def draft_node(state: dict, model: ReplyModel, on_delta=None) -> dict:
    """起草。`on_delta(delta, reset)` 存在时走真流式（远程模型的首 token 延迟靠它）。"""

    persona = PersonaCard.from_dict(state["persona"])
    memory = ShortTermMemory(
        turns=state.get("short_memory", []),
        summary=str(state.get("memory_summary", "")),
    )
    hits = [HistoryHit.from_dict(value) for value in state.get("retrieved", [])]

    critique = state.get("critique") or {}
    is_rewrite = critique.get("verdict") == "rewrite"
    feedback = ""
    if is_rewrite:
        feedback = "上一版的问题：" + "、".join(critique.get("problems", []))

    generate_kwargs = {
        "user_input": str(state.get("user_input", "")),
        "persona": persona,
        "retrieved": hits,
        "memory": memory,
        "feedback": feedback,
    }

    streamer = getattr(model, "generate_stream", None)
    if on_delta is not None and callable(streamer):
        if is_rewrite:
            # 重写时先让客户端清空上一版，否则两版正文会拼在一起。
            on_delta("", True)
        parts: List[str] = []
        for delta in streamer(**generate_kwargs):
            if not delta:
                continue
            parts.append(delta)
            on_delta(delta, False)
        draft = "".join(parts).strip()
        if not draft:
            draft = model.generate(**generate_kwargs)
    else:
        draft = model.generate(**generate_kwargs)

    if is_rewrite and memory.turns:
        # 重写是"替换这一轮"，不是"再来一轮"，否则记忆里会留下重复对话。
        memory.replace_last_reply(draft)
        turn_count = int(state.get("turn_count", 0))
    else:
        memory.add_turn(str(state.get("user_input", "")), draft)
        turn_count = int(state.get("turn_count", 0)) + 1

    return {
        "draft": draft,
        "reply": draft,
        "short_memory": memory.to_list(),
        "memory_summary": memory.summary,
        "turn_count": turn_count,
    }


# --------------------------------------------------------------------------
# Critic：先规则、后模型。能代码判的绝不用 LLM。
# --------------------------------------------------------------------------
def copied_from_other(
    reply: str, other_messages: Sequence[str], window: int = CROSSTALK_WINDOW
) -> bool:
    """Return True when the reply copies a long verbatim span from the other speaker."""

    if len(reply) < window:
        return False
    blob = "\n".join(other_messages)
    if not blob:
        return False
    return any(
        reply[index : index + window] in blob
        for index in range(len(reply) - window + 1)
    )


def sensitive_categories(reply: str) -> List[str]:
    """Return the sensitive categories a reply touches (empty when clean)."""

    return [
        category
        for category, patterns in SENSITIVE_PATTERNS.items()
        if any(pattern in reply for pattern in patterns)
    ]


def critic_node(
    state: dict,
    other_messages: Sequence[str] = (),
    max_rewrite: int = 2,
    llm_check=None,
) -> dict:
    """规则判定 +（可选）模型复核。

    `llm_check` 只在 `CRITIC_LLM=1` 时传入。它补的是规则查不出的"无数字编造"：
    "我们上个月办过婚礼"没有数字、没有敏感词，规则会放行，但它确实不在证据里。
    复核失败一律跳过——判定不该成为主链路的失败点。
    """
    reply = str(state.get("reply", "")).strip()
    persona = PersonaCard.from_dict(state["persona"])
    problems: List[str] = []

    if not reply:
        problems.append("empty")

    lowered = reply.lower()
    if any(token in lowered for token in ASSISTANT_TONE):
        problems.append("assistant_tone")

    # 人设卡里的自定义禁用词（Day13）：与通用助手腔分开报，便于定位是谁的锅。
    custom = [
        token
        for token in (persona.forbidden or [])
        if token.lower() not in ASSISTANT_TONE and token.lower() in lowered
    ]
    if custom:
        problems.append("forbidden_phrase")

    limit = max(16, int(persona.average_message_length * 3))
    if len(reply) > limit:
        problems.append("too_long")

    if copied_from_other(reply, other_messages):
        problems.append("crosstalk")

    evidence = " ".join(
        f"{hit.get('content', '')} {hit.get('context', '')}"
        for hit in state.get("retrieved", [])
    )
    if any(number not in evidence for number in _NUMBER.findall(reply)):
        problems.append("unsupported_fact")

    categories = sensitive_categories(reply)
    if categories:
        problems.append("sensitive")

    # 规则查不出的编造：只对"带事实断言"的回复复核，省调用也避免误伤语气句。
    if llm_check is not None and not problems:
        try:
            from evaluator.faithfulness import is_factual

            claims = [claim for claim in re.split(r"[。！？；!?\n]+", reply) if claim.strip()]
            if any(is_factual(claim) for claim in claims):
                supported, _reason = llm_check(reply, evidence)
                if not supported:
                    problems.append("unsupported_fact_llm")
        except Exception:  # noqa: BLE001 - 复核失败不影响规则结论
            pass

    turns = state.get("short_memory", [])
    if len(turns) >= 2 and reply == turns[-2].get("assistant_reply", ""):
        problems.append("repeated")

    rewrites = int(state.get("rewrite_count", 0))
    if not problems:
        verdict = "pass"
    elif "sensitive" in problems:
        # 敏感内容不重写：换一种说法还是在替你答应一件事。
        verdict = "review"
    elif rewrites >= max_rewrite:
        verdict = "review"
    else:
        verdict = "rewrite"

    return {
        "critique": {
            "verdict": verdict,
            "problems": problems,
            "categories": categories,
            "rewrites": rewrites,
        },
        "rewrite_count": rewrites + (1 if verdict == "rewrite" else 0),
    }


def route_after_critic(state: dict) -> str:
    """Conditional edge: pass → END, rewrite → draft, review → human_review."""

    return str((state.get("critique") or {}).get("verdict", "pass"))


def review_payload(state: dict) -> dict:
    """中断时交给人的待决信息。两个引擎（自研 / LangGraph）共用。"""

    critique = state.get("critique") or {}
    problems = list(critique.get("problems", []))
    categories = list(critique.get("categories", []))

    if "sensitive" in problems:
        reason = "sensitive"
        question = "这条回复涉及敏感内容，需要你确认是否发送。"
    else:
        reason = "rewrite_budget"
        question = "两次重写仍未通过检查，需要你改写或拒绝。"

    return {
        "node": "human_review",
        "reason": reason,
        "question": question,
        "problems": problems,
        "categories": categories,
        "reply": str(state.get("reply", "")),
        "user_input": str(state.get("user_input", "")),
        "allowed_actions": list(VALID_ACTIONS),
    }


def apply_decision(state: dict, decision: dict) -> dict:
    """把人工裁决写回状态。两个引擎共用，避免规则分叉。"""

    action = str(decision.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {list(VALID_ACTIONS)}, got {action!r}")

    updates: dict = {}
    if action == "approve":
        updates["status"] = "ok"
    elif action == "edit":
        reply = str(decision.get("reply", "")).strip()
        if not reply:
            raise ValueError("edit requires a non-empty reply")
        updates.update(status="ok", reply=reply, draft=reply)
    else:  # reject
        updates.update(status="rejected", reply=DEGRADED_REPLY, draft=DEGRADED_REPLY)

    updates["human_decision"] = action
    updates["pending"] = None
    updates["critique"] = {**(state.get("critique") or {}), "human": action}

    memory = ShortTermMemory(turns=state.get("short_memory", []))
    if memory.turns:
        memory.replace_last_reply(updates.get("reply", str(state.get("reply", ""))))
        updates["short_memory"] = memory.to_list()
    return updates


# --------------------------------------------------------------------------
# HumanReview（Day11）：不再静默降级，而是抛出中断，等人裁决。
# --------------------------------------------------------------------------
def human_review_node(state: dict) -> dict:
    raise GraphInterrupt(review_payload(state))
