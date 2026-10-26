"""测试用的确定性模型（实现已移到 `llm_client/scripted.py`）。

保留这个别名是为了让测试里的 import 保持稳定：
`from tests.fakes import ScriptedModel`。
"""

from __future__ import annotations

from llm_client.scripted import ScriptedModel

__all__ = ["ScriptedModel"]
