"""Serializable state carried through the Persona Replay graph.

Day8 新增字段：
- `chat_id`         导入会话的引用（派生数据放 ingest.session_store，不进 state）
- `persona_ready`   Planner 只跑一次的标记
- `critique`        Critic 的结构化判定结果（也是重写时的输入）
- `rewrite_count`   重写次数，与图的步数预算共同防死循环
- `status`          ok / escalated / budget_exhausted
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentState:
    thread_id: str
    chat_path: str
    user_speaker: str
    target_speaker: str
    user_input: str
    speakers: List[str] = field(default_factory=list)
    persona: Optional[dict] = None
    retrieved: List[dict] = field(default_factory=list)
    draft: str = ""
    reply: str = ""
    critique: Optional[dict] = None
    rewrite_count: int = 0
    short_memory: List[dict] = field(default_factory=list)
    memory_summary: str = ""
    steps: List[str] = field(default_factory=list)
    turn_count: int = 0
    chat_id: str = ""
    persona_ready: bool = False
    status: str = "ok"
    pending: Optional[dict] = None
    human_decision: Optional[str] = None
    # 观测：一轮对话共享 trace_id，事件各自带 span_id（OTel 风格）
    trace_id: str = ""
    root_span_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "AgentState":
        return cls(
            thread_id=str(value.get("thread_id", "")),
            chat_path=str(value.get("chat_path", "")),
            user_speaker=str(value.get("user_speaker", "")),
            target_speaker=str(value.get("target_speaker", "")),
            user_input=str(value.get("user_input", "")),
            speakers=list(value.get("speakers", [])),
            persona=value.get("persona"),
            retrieved=list(value.get("retrieved", [])),
            draft=str(value.get("draft", "")),
            reply=str(value.get("reply", "")),
            critique=value.get("critique"),
            rewrite_count=int(value.get("rewrite_count", 0)),
            short_memory=list(value.get("short_memory", [])),
            memory_summary=str(value.get("memory_summary", "")),
            steps=list(value.get("steps", [])),
            turn_count=int(value.get("turn_count", 0)),
            chat_id=str(value.get("chat_id", "")),
            persona_ready=bool(value.get("persona_ready", False)),
            status=str(value.get("status", "ok")),
            pending=value.get("pending"),
            human_decision=value.get("human_decision"),
            trace_id=str(value.get("trace_id", "")),
            root_span_id=str(value.get("root_span_id", "")),
        )
