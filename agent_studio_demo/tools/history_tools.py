"""Read-only tools used by the hand-written ReAct loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict

from memory.persona import build_persona_card
from rag.history_index import HistoryIndex
from tools.chat_parser import parse_chat_file


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    handler: Callable[[str], str]

    def use(self, input_text: str) -> str:
        return self.handler(input_text)


class ToolRegistry:
    """Explicit allow-list: unknown tools, including send tools, are rejected."""

    def __init__(self, tools: list[FunctionTool]):
        self._tools: Dict[str, FunctionTool] = {tool.name: tool for tool in tools}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def describe(self) -> str:
        return "\n".join(
            f"{tool.name}: {tool.description}" for tool in self._tools.values()
        )

    def invoke(self, name: str, input_text: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(
                f"tool {name!r} is not allowed; allowed tools: {list(self._tools)}"
            )
        return tool.use(input_text)


def _payload(input_text: str) -> dict:
    try:
        value = json.loads(input_text)
    except json.JSONDecodeError as exc:
        raise ValueError("tool input must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("tool input must be a JSON object")
    return value


def _chat_path(value: dict) -> Path:
    path = value.get("path")
    if not path:
        raise ValueError("tool input requires path")
    return Path(str(path))


def _parse_chat(input_text: str) -> str:
    value = _payload(input_text)
    parsed = parse_chat_file(_chat_path(value))
    return json.dumps(
        {
            "speakers": list(parsed.speakers),
            "message_count": len(parsed.messages),
            "speaker_counts": parsed.speaker_counts,
        },
        ensure_ascii=False,
    )


def _search_history(input_text: str) -> str:
    value = _payload(input_text)
    parsed = parse_chat_file(_chat_path(value))
    first, second = parsed.require_two_speakers()
    target = str(value.get("target_speaker") or second)
    user = str(value.get("user_speaker") or first)
    index = HistoryIndex.from_chat(parsed, target, user)
    hits = index.search(str(value.get("query", "")), int(value.get("limit", 3)))
    return json.dumps(
        {"target_speaker": target, "hits": [hit.to_dict() for hit in hits]},
        ensure_ascii=False,
    )


def _get_persona(input_text: str) -> str:
    value = _payload(input_text)
    parsed = parse_chat_file(_chat_path(value))
    _, default_target = parsed.require_two_speakers()
    target = str(value.get("target_speaker") or default_target)
    return json.dumps(
        build_persona_card(parsed, target).to_dict(), ensure_ascii=False
    )


def default_tool_registry() -> ToolRegistry:
    """Return the Day2-Day4 read-only tool set."""

    return ToolRegistry(
        [
            FunctionTool(
                "parse_chat",
                "Parse a local UTF-8 two-person chat export and list speakers.",
                _parse_chat,
            ),
            FunctionTool(
                "search_history",
                "Search target-speaker historical replies and return evidence.",
                _search_history,
            ),
            FunctionTool(
                "get_persona",
                "Extract an explainable local persona card for one speaker.",
                _get_persona,
            ),
        ]
    )
