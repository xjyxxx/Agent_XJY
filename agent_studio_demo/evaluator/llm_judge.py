"""Day12：10 轮固定对白的离线评估。

三层里这里实现的是 **L1 客观指标**（默认 RuleJudge）和一个可选的 **L2 LLM judge**。
L1 的价值不是"证明效果"，而是**回归门禁**：改 prompt / 换模型前后跑同一套样例，
指标掉了就拦住。统计上 10 个样本的 95% 置信区间宽度约 ±30 个百分点，所以
不要拿它宣称"提升 X%"（见 README 的诚实边界）。

RuleJudge 的计算路径与被评估的 Critic 独立（见 `evaluator/rules.py`），
但两者都基于规则，所以 L1 只能捕捉**已知类型**的退化，捕捉不到"更微妙的不像"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from agent.graph import PersonaReplayRuntime
from agent.nodes import ASSISTANT_TONE, copied_from_other
from evaluator.faithfulness import aggregate as aggregate_faithfulness
from evaluator.faithfulness import score as faithfulness_score
from evaluator.rules import (
    assistant_tone_hits,
    style_similarity,
    unsupported_numbers,
)
from ingest.session_store import get_session
from llm_client.heuristic import HeuristicReplyModel, ReplyModel
from llm_client.scripted import ScriptedModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "fixtures" / "chat_laowang_xiaolin.txt"
ROUNDS_FILE = PROJECT_ROOT / "fixtures" / "eval_rounds.json"

JUDGE_SYSTEM = """你是一个严格的中文聊天记录评审。只输出 JSON，不要解释。
JSON 字段：
- like_original: true/false，这条回复像不像聊天记录里该角色的真实说话方式
- crosstalk: true/false，是否用了另一个人的口吻
- hallucination: true/false，是否编造了证据里没有的事实、数字或承诺
"""

JUDGE_USER = """角色人设：
{persona}

检索到的历史证据：
{evidence}

用户刚说：{user_input}
角色回复：{reply}
"""


@dataclass
class TurnMetrics:
    """一轮的独立观测结果。"""

    style_similarity: float = 0.0
    crosstalk: bool = False
    hallucination: bool = False
    assistant_tone: bool = False
    evidence_hits: int = 0
    notes: Dict[str, object] = field(default_factory=dict)


@dataclass
class RoundRecord:
    """一轮的完整评估记录。"""

    round_id: str
    user_input: str
    reply: str
    status: str
    verdict: str
    problems: List[str]
    tags: List[str]
    metrics: TurnMetrics
    expectation_ok: bool
    expect_human: bool = False
    failures: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        value = asdict(self)
        return value


class RuleJudge:
    """L1：纯规则、零成本、可进 CI。"""

    name = "rule"

    def assess(self, state: dict, other_messages: Sequence[str] = ()) -> TurnMetrics:
        reply = str(state.get("reply", ""))
        persona = state.get("persona") or {}
        evidence = [
            f"{hit.get('content', '')} {hit.get('context', '')}"
            for hit in state.get("retrieved", [])
        ]
        return TurnMetrics(
            style_similarity=style_similarity(reply, persona),
            crosstalk=copied_from_other(reply, other_messages),
            hallucination=bool(unsupported_numbers(reply, evidence)),
            assistant_tone=bool(assistant_tone_hits(reply, ASSISTANT_TONE)),
            evidence_hits=len(state.get("retrieved", [])),
        )


class LLMJudge:
    """L2：让模型按固定 rubric 判二元项，成本低、方差大，用于方向判断。"""

    name = "llm"

    def __init__(self, call: Optional[Callable[[str, str], str]] = None):
        self._call = call

    def assess(self, state: dict, other_messages: Sequence[str] = ()) -> TurnMetrics:
        if self._call is None:
            raise RuntimeError(
                "LLMJudge 需要注入 call(system, user) -> str；"
                "未配置模型时请使用 --judge rule"
            )
        persona = state.get("persona") or {}
        evidence = "\n".join(
            f"- {hit.get('content', '')}（上文：{hit.get('context', '')}）"
            for hit in state.get("retrieved", [])
        ) or "（无）"
        user = JUDGE_USER.format(
            persona=json.dumps(persona, ensure_ascii=False),
            evidence=evidence,
            user_input=state.get("user_input", ""),
            reply=state.get("reply", ""),
        )
        raw = self._call(JUDGE_SYSTEM, user)
        try:
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"LLM judge 返回的不是 JSON：{raw[:200]!r}") from exc

        return TurnMetrics(
            style_similarity=1.0 if parsed.get("like_original") else 0.0,
            crosstalk=bool(parsed.get("crosstalk")),
            hallucination=bool(parsed.get("hallucination")),
            assistant_tone=bool(assistant_tone_hits(str(state.get("reply", "")), ASSISTANT_TONE)),
            evidence_hits=len(state.get("retrieved", [])),
            notes={"raw": parsed},
        )


def load_rounds(path: Optional[Path] = None) -> List[dict]:
    rounds_path = Path(path or ROUNDS_FILE)
    data = json.loads(rounds_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"eval rounds must be a non-empty list: {rounds_path}")
    return data


def evaluate(
    rounds: Iterable[dict],
    judge,
    default_model: Optional[ReplyModel] = None,
    chat_path: Path | str = FIXTURE,
    checkpoint: str = ":memory:",
) -> List[RoundRecord]:
    """Run every round through the real graph and score it."""

    base_model = default_model or HeuristicReplyModel()
    records: List[RoundRecord] = []

    for config in rounds:
        model_reply = config.get("model_reply")
        model = ScriptedModel([model_reply]) if model_reply else base_model
        runtime = PersonaReplayRuntime(checkpoint, model=model)
        try:
            state = runtime.invoke(
                thread_id=f"eval-{config['id']}",
                chat_path=str(chat_path),
                user_speaker="老王",
                target_speaker="小林",
                user_input=config["user_input"],
            )
            raw = state.to_dict()
            session = get_session(state.chat_id)
            other = session.other_speaker_messages() if session else ()
            metrics = judge.assess(raw, other)
            # RAGAS 式 faithfulness：只统计"有事实断言"的轮次，语气句算 n/a。
            evidence = []
            for item in state.retrieved or []:
                if isinstance(item, dict):
                    evidence.extend([str(item.get("content", "")), str(item.get("context", ""))])
                else:
                    evidence.extend(
                        [
                            str(getattr(item, "content", "")),
                            str(getattr(item, "context", "")),
                        ]
                    )
            metrics.notes["faithfulness"] = faithfulness_score(
                state.reply, [text for text in evidence if text]
            )
        finally:
            runtime.close()

        problems = list((state.critique or {}).get("problems", []))
        failures = _expectation_failures(config, state, problems)
        records.append(
            RoundRecord(
                round_id=str(config["id"]),
                user_input=str(config["user_input"]),
                reply=state.reply,
                status=state.status,
                verdict=str((state.critique or {}).get("verdict", "")),
                problems=problems,
                tags=list(config.get("tags", [])),
                metrics=metrics,
                expectation_ok=not failures,
                expect_human=bool(config.get("expect_human", False)),
                failures=failures,
            )
        )
    return records


def _expectation_failures(config: dict, state, problems: Sequence[str]) -> List[str]:
    failures: List[str] = []
    expect_evidence = bool(config.get("expect_evidence", False))
    has_evidence = len(state.retrieved) > 0
    if has_evidence != expect_evidence:
        failures.append(f"evidence:{has_evidence}!=expect:{expect_evidence}")

    expect_human = bool(config.get("expect_human", False))
    needs_human = state.status in {"interrupted", "rejected"}
    if needs_human != expect_human:
        failures.append(f"human:{needs_human}!=expect:{expect_human}")

    expect_problem = config.get("expect_problem")
    if expect_problem and expect_problem not in problems:
        failures.append(f"problem:{expect_problem} not in {problems}")
    return failures


def aggregate(records: Sequence[RoundRecord]) -> dict:
    """Split clean rounds from adversarial ones — they measure different things.

    干净轮次衡量"质量"（像不像、有没有串台/编造），对抗轮次衡量"拦截率"。
    把两者混在一个比率里，会让"系统正确拦住了一次编造"被算成"系统失败了"，
    这是第一版评估器犯的错。
    """

    if not records:
        raise ValueError("no records to aggregate")

    clean = [record for record in records if not record.expect_human]
    adversarial = [record for record in records if record.expect_human]

    def rate(group: Sequence[RoundRecord], predicate) -> float:
        if not group:
            return 1.0
        return round(sum(1 for record in group if predicate(record)) / len(group), 4)

    return {
        "rounds": len(records),
        "clean_rounds": len(clean),
        "adversarial_rounds": len(adversarial),
        "expectation_pass_rate": rate(records, lambda record: record.expectation_ok),
        "style_similarity": round(
            sum(record.metrics.style_similarity for record in clean) / len(clean), 4
        )
        if clean
        else 0.0,
        "crosstalk_rate": rate(clean, lambda record: record.metrics.crosstalk),
        "hallucination_rate": rate(clean, lambda record: record.metrics.hallucination),
        "assistant_tone_rate": rate(clean, lambda record: record.metrics.assistant_tone),
        "catch_rate": rate(
            adversarial,
            lambda record: record.status in {"interrupted", "rejected"}
            and bool(record.problems),
        ),
        "human_review_rate": rate(
            records, lambda record: record.status in {"interrupted", "rejected"}
        ),
        "evidence_coverage": round(
            sum(record.metrics.evidence_hits for record in clean) / len(clean), 4
        )
        if clean
        else 0.0,
        # 借 RAGAS 的定义：事实断言里能被证据支持的比例；全是语气句时是 n/a。
        "faithfulness": aggregate_faithfulness(
            [record.metrics.notes.get("faithfulness") for record in clean]
        ),
        # 对抗轮次才是"编不编造"的高发区，单独给一份。
        "faithfulness_adversarial": aggregate_faithfulness(
            [record.metrics.notes.get("faithfulness") for record in adversarial]
        ),
    }


DEFAULT_THRESHOLDS = {
    "expectation_pass_rate": 1.0,
    "catch_rate": 1.0,
    "crosstalk_rate": 0.0,
    "hallucination_rate": 0.0,
    "assistant_tone_rate": 0.0,
    # 0.45 是当前基线（启发式模型不是好文风），不是目标值。换真模型后应该上调。
    "style_similarity": 0.45,
}

# 这些指标越低越好；其余阈值都是"不能低于"。
LOWER_IS_BETTER = {"crosstalk_rate", "hallucination_rate", "assistant_tone_rate"}


def gate(metrics: dict, thresholds: Optional[Dict[str, float]] = None) -> List[str]:
    """Return the failed checks (empty list means the gate passed)."""

    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    failures: List[str] = []
    for key, limit in limits.items():
        value = metrics.get(key)
        if value is None:
            continue
        if key in LOWER_IS_BETTER:
            if float(value) > float(limit):
                failures.append(f"{key}={value} > {limit}")
        elif float(value) < float(limit):
            failures.append(f"{key}={value} < {limit}")
    return failures
