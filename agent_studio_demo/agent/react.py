"""A small, inspectable ReAct loop for Day2.

The third-party snapshot is used only for reading. This implementation keeps
the same Thought -> Action -> Observation shape but uses a deterministic
policy, so it can run without API credentials.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from tools.history_tools import ToolRegistry, default_tool_registry


FINAL_ANSWER_TOKEN = "Final Answer:"
OBSERVATION_TOKEN = "Observation:"
THOUGHT_TOKEN = "Thought:"
_ACTION_RE = re.compile(r"Action:\s*\[?(.*?)\]?\s*\nAction Input:\s*(.*)", re.DOTALL)


@dataclass
class ReActStep:
    thought: str
    action: str
    action_input: str
    observation: str


@dataclass
class ReActResult:
    answer: str
    steps: List[ReActStep] = field(default_factory=list)


class ReActAgent:
    """Hand-written ReAct loop with an explicit read-only tool registry."""

    def __init__(
        self,
        tools: Optional[ToolRegistry] = None,
        max_loops: int = 4,
        policy: Optional[Callable[[str, List[ReActStep], dict], str]] = None,
    ):
        if max_loops < 1:
            raise ValueError("max_loops must be at least 1")
        self.tools = tools or default_tool_registry()
        self.max_loops = max_loops
        self.policy = policy or self._default_policy

    @staticmethod
    def parse_action(generated: str) -> Tuple[str, str]:
        if FINAL_ANSWER_TOKEN in generated:
            return "Final Answer", generated.split(FINAL_ANSWER_TOKEN, 1)[1].strip()
        match = _ACTION_RE.search(generated)
        if not match:
            raise ValueError(f"unparsable ReAct output: {generated!r}")
        return match.group(1).strip(), match.group(2).strip().strip('"')

    def run(
        self,
        question: str,
        chat_path: str | Path,
        user_speaker: str,
        target_speaker: str,
    ) -> ReActResult:
        context = {
            "path": str(chat_path),
            "query": question,
            "user_speaker": user_speaker,
            "target_speaker": target_speaker,
        }
        steps: List[ReActStep] = []
        for _ in range(self.max_loops):
            generated = self.policy(question, steps, context)
            action, action_input = self.parse_action(generated)
            if action == "Final Answer":
                return ReActResult(answer=action_input, steps=steps)
            observation = self.tools.invoke(action, action_input)
            thought = generated.split("Action:", 1)[0].replace(THOUGHT_TOKEN, "").strip()
            steps.append(
                ReActStep(
                    thought=thought,
                    action=action,
                    action_input=action_input,
                    observation=observation,
                )
            )
        raise RuntimeError("ReAct loop exceeded max_loops without a final answer")

    @staticmethod
    def _default_policy(question: str, steps: List[ReActStep], context: dict) -> str:
        actions = {step.action for step in steps}
        if "parse_chat" not in actions:
            payload = json.dumps({"path": context["path"]}, ensure_ascii=False)
            return f"Thought: 先解析聊天记录。\nAction: parse_chat\nAction Input: {payload}"
        if "search_history" not in actions:
            payload = json.dumps(context, ensure_ascii=False)
            return (
                "Thought: 再检索目标角色的历史回复作为证据。\n"
                f"Action: search_history\nAction Input: {payload}"
            )
        if "get_persona" not in actions:
            payload = json.dumps(
                {"path": context["path"], "target_speaker": context["target_speaker"]},
                ensure_ascii=False,
            )
            return (
                "Thought: 最后读取目标角色的人设摘要。\n"
                f"Action: get_persona\nAction Input: {payload}"
            )
        last = json.loads(steps[-2].observation) if len(steps) >= 2 else {}
        hits = last.get("hits", [])
        evidence = hits[0]["content"] if hits else "无匹配历史回复"
        return f"Thought: 已经获得证据。\nFinal Answer: 历史参考：{evidence}"
