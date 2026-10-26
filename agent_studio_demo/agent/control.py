"""图执行中断（Day11）。

节点通过抛 `GraphInterrupt` 暂停整张图，而不是返回一个"降级回复"。
这个区别很重要：

- 返回降级回复 = 系统替人做了决定，且决定不可见；
- 抛中断 = 系统把**待决状态**交给调度器，由调度器落盘、由人裁决、再恢复。

调度器在捕获中断时会补上 `resume_at`（当前节点的后继），这样恢复时能从
中断点之后继续，而不是把整轮重跑一遍。
"""

from __future__ import annotations

from typing import Any, Dict


class GraphInterrupt(Exception):
    """Raised by a node to pause the graph and hand control to a human."""

    def __init__(self, payload: Dict[str, Any]):
        super().__init__(str(payload.get("reason", "interrupted")))
        self.payload: Dict[str, Any] = dict(payload)
