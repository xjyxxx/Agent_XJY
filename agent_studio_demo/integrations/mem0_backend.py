"""mem0 可选后端：借它的"事实抽取 + 去重 + 更新"能力。

什么时候值得开：真实对话里事实多、跨很多轮（比如几百轮以上）。
本项目的样例只有 20 条消息，打开它只会多花钱和引入不确定性，所以**默认关闭**。

用法：
    pip install mem0ai
    [features] memory_backend = mem0     # config/settings.ini

注意：mem0 需要配置 LLM 和 embedding（它自己会调模型抽事实）。
这里只做薄封装，真正的配置由调用方传入，避免我们替用户选供应商。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from integrations.optional import DependencyMissing, module_available


def mem0_available() -> bool:
    return module_available("mem0")


class Mem0Store:
    """把 mem0 包成 `add / search` 两个方法，便于主链路按需替换。"""

    name = "mem0"

    def __init__(
        self,
        user_id: str = "persona-replay",
        config: Optional[Dict[str, Any]] = None,
        memory=None,
    ):
        if memory is not None:  # 测试注入
            self._memory = memory
        else:
            if not mem0_available():
                raise DependencyMissing(
                    "缺少可选依赖 mem0。安装：pip install mem0ai\n"
                    "（不装也能用：项目自带短期记忆 + 滚动摘要，见 memory/persona.py）"
                )
            from mem0 import Memory  # noqa: PLC0415 - 可选依赖，用到才导入

            self._memory = Memory.from_config(config) if config else Memory()
        self.user_id = user_id

    def add_turn(self, user_text: str, reply: str) -> None:
        messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        self._memory.add(messages, user_id=self.user_id)

    def search(self, query: str, limit: int = 3) -> List[str]:
        result = self._memory.search(query, user_id=self.user_id, limit=limit)
        # mem0 各版本返回 {"results": [...]} 或直接 list
        items = result.get("results", []) if isinstance(result, dict) else result or []
        memories: List[str] = []
        for item in items:
            if isinstance(item, dict):
                text = item.get("memory") or item.get("text") or ""
            else:
                text = str(item)
            if text:
                memories.append(str(text))
        return memories[:limit]

    def reset(self) -> None:
        reset = getattr(self._memory, "reset", None)
        if callable(reset):
            reset()


__all__ = ["Mem0Store", "mem0_available"]
