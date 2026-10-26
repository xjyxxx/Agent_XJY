"""引擎选择：官方 LangGraph 优先，装不上就退回自研同构实现。

放在单独模块里是为了**不破坏零依赖**：`agent.langgraph_runtime` 在顶层 import
langgraph，如果被 `api.service` 直接 import，没装 langgraph 的机器连服务都起不来。
所以这里做懒加载，并把"装坏了"也兜住。
"""

from __future__ import annotations

import os
from typing import Optional

from llm_client.heuristic import ReplyModel


ENGINES = ("auto", "langgraph", "builtin")


def engine_hint() -> str:
    return os.environ.get("GRAPH_ENGINE", "auto").strip().lower() or "auto"


def langgraph_available() -> bool:
    """真 import 一次，而不是 find_spec。

    `find_spec` 只查 finder，不看"这次导入是否真的会失败"（例如依赖被打桩、
    或 langchain-core 版本不匹配时，spec 存在但 import 会炸）。测试和 fallback
    都应该基于"能不能 import"，否则会走进只有运行到一半才失败的分支。
    """

    try:
        from langgraph.graph import StateGraph  # noqa: F401
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def create_runtime(
    checkpoint_path: str = ":memory:",
    model: Optional[ReplyModel] = None,
    max_steps: int = 12,
    engine: Optional[str] = None,
):
    """按 `GRAPH_ENGINE`（或显式参数）造 runtime。"""

    name = (engine or engine_hint()).strip().lower()
    if name not in ENGINES:
        raise ValueError(f"unknown GRAPH_ENGINE: {name!r} ({'|'.join(ENGINES)})")

    if name == "builtin":
        from agent.graph import PersonaReplayRuntime

        return PersonaReplayRuntime(checkpoint_path, model=model, max_steps=max_steps)

    if name == "langgraph":
        from agent.langgraph_runtime import build_runtime

        return build_runtime("langgraph", checkpoint_path, model=model, max_steps=max_steps)

    # auto：优先官方引擎；任何导入/构造失败都退回自研实现
    try:
        from agent.langgraph_runtime import build_runtime

        return build_runtime("auto", checkpoint_path, model=model, max_steps=max_steps)
    except Exception:  # noqa: BLE001 - 装坏了也不该让服务起不来
        from agent.graph import PersonaReplayRuntime

        return PersonaReplayRuntime(checkpoint_path, model=model, max_steps=max_steps)
