"""Select reply model: heuristic (local) or deepseek (cloud API)."""

from __future__ import annotations

import os
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
    """Load KEY=VALUE lines from .env into os.environ (does not override existing)."""

    env_path = path or os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
