"""Select reply model: heuristic (local) or deepseek (cloud API)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from llm_client.heuristic import HeuristicReplyModel, ReplyModel


def create_reply_model(provider: str = "auto") -> ReplyModel:
    """Create a reply model.

    provider:
      - auto: DeepSeek if DEEPSEEK_API_KEY is set, else heuristic
      - heuristic: local deterministic model (no network)
      - deepseek: require DEEPSEEK_API_KEY
    """

    name = (provider or "auto").strip().lower()
    if name == "heuristic":
        return HeuristicReplyModel()
    if name == "deepseek":
        from llm_client.deepseek import DeepSeekReplyModel

        return DeepSeekReplyModel()
    if name == "auto":
        if os.environ.get("DEEPSEEK_API_KEY", "").strip():
            from llm_client.deepseek import DeepSeekReplyModel

            return DeepSeekReplyModel()
        return HeuristicReplyModel()
    raise ValueError(f"未知 provider: {provider!r}，可选 auto / heuristic / deepseek")


def load_dotenv(path: Optional[str] = None) -> None:
    """兼容旧入口：现在统一走 `config.bootstrap()`。

    顺序：`config/settings.ini`（推荐） → `.env`（旧方式） → 不覆盖已有环境变量。
    这样所有老调用点（CLI / API / 评估 / 预检）不用改就自动支持配置文件。
    """

    from config.loader import apply, bootstrap, read_env_file

    if path:
        # 显式指定文件时保持旧语义：只读这个 .env
        apply(read_env_file(Path(path)))
        return
    bootstrap()
