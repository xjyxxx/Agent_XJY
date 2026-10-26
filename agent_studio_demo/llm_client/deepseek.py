"""OpenAI 兼容的 Chat Completions 适配器（默认对接 DeepSeek）。

虽然文件名叫 deepseek，但它只依赖 OpenAI 的 `/chat/completions` 协议，所以换
`DEEPSEEK_BASE_URL` 就能接任何兼容端点（DeepSeek、月之暗面、vLLM、Ollama 的
OpenAI 兼容层、公司内网网关…）。**不下载本地模型也能跑真模型**，就是靠这一层。

环境变量：
    DEEPSEEK_API_KEY   必填
    DEEPSEEK_BASE_URL  默认 https://api.deepseek.com（可以带 /v1，会自动去掉）
    DEEPSEEK_MODEL     默认 deepseek-flash

两个公开能力：
- `generate()`        一次拿完整回复
- `generate_stream()` 逐块拿回复（真流式，SSE）
- `probe()`           连通性/鉴权/余额体检（`python -m llm_client.check`）

隐私边界：走远程 API 意味着对话内容会离开本机。仓库只用合成样例，所以没问题；
真实私聊记录请不要配远程模型（见 README「隐私与远程模型」）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterator, List, Optional

from memory.persona import PersonaCard, ShortTermMemory
from rag.history_index import HistoryHit


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class DeepSeekReplyModel:
    """Call an OpenAI-compatible chat endpoint to draft a persona-style reply."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        on_usage=None,
        temperature: float = 0.7,
    ):
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError(
                "缺少 DEEPSEEK_API_KEY。请先设置环境变量，或在 .env 中配置"
                "（复制 .env.example 即可）。"
            )
        raw_base = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        # Accept both https://api.deepseek.com and .../v1
        if raw_base.endswith("/v1"):
            self.base_url = raw_base[: -len("/v1")]
        else:
            self.base_url = raw_base
        self.model = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        # 生成默认 0.7（有变化才像人）；评估必须设 0（否则同一轮两次跑出不同结果，
        # 指标差异会被采样噪声淹没——这是实测踩到的）。
        self.temperature = temperature
        # 埋点：每轮 token 用量（成本）与首 token 延迟（流式）。
        # 回调在调用线程里同步触发；last_ttft_ms 是"最近一次"的近似值，
        # 并发场景下应按请求取回调参数，不要依赖这个属性。
        self.on_usage = on_usage
        self.last_usage: dict = {}
        self.last_ttft_ms: Optional[float] = None

    # -- public API ------------------------------------------------------
    def generate(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> str:
        system, user = self._build_prompts(user_input, persona, retrieved, memory, feedback)
        return self._chat(system=system, user=user, temperature=self.temperature).strip() or "嗯"

    def generate_stream(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> Iterator[str]:
        """Yield content deltas as they arrive (real token-level streaming)."""

        system, user = self._build_prompts(user_input, persona, retrieved, memory, feedback)
        return self._chat_stream(system=system, user=user, temperature=self.temperature)

    def probe(self, timeout: float = 15.0) -> dict:
        """Connectivity / auth / balance check with one tiny request."""

        started = time.perf_counter()
        reply = self._chat(
            system="你是一个连通性探针，只回两个字母：OK",
            user="ping",
            temperature=0.0,
            timeout=timeout,
        )
        return {
            "ok": True,
            "model": self.model,
            "base_url": self.base_url,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "reply": reply.strip()[:40],
        }

    @classmethod
    def config_summary(cls) -> dict:
        """Show what would be used, without leaking the key."""

        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if key:
            hint = f"{key[:3]}***{key[-4:]}" if len(key) > 8 else "***"
        else:
            hint = ""
        base = (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        return {
            "base_url": base,
            "model": os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
            "key_present": bool(key),
            "key_hint": hint,
        }

    # -- prompt building -------------------------------------------------
    def _build_prompts(
        self,
        user_input: str,
        persona: PersonaCard,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str,
    ) -> tuple[str, str]:
        return (
            self._build_system_prompt(persona),
            self._build_user_prompt(user_input, retrieved, memory, feedback),
        )

    def _build_system_prompt(self, persona: PersonaCard) -> str:
        terms = "、".join(persona.common_terms[:8]) or "无"
        punct = "、".join(persona.punctuation_style[:5]) or "无"
        examples = "\n".join(f"- {ex}" for ex in persona.example_replies[:5]) or "- （无）"
        addressing = "、".join(persona.addressing) or "（未识别，用「你」即可）"
        catchphrases = "、".join(persona.catchphrases) or "无"
        relationship = persona.relationship or "未指定"
        forbidden = "、".join(persona.forbidden[:6]) or "无"
        return f"""你在扮演聊天记录里的「{persona.speaker}」。
必须遵守：
1. 只用对方的语气、用词和回复习惯；短句优先，贴近平均长度约 {persona.average_message_length:.0f} 字。
2. 优先参考下方检索到的历史原句；没有依据时不要编造事实、承诺或隐私。
3. 禁止助手腔（例如「作为AI」「我可以帮你」）。
4. 不要串台成另一个人的口吻。
5. 只输出回复正文，不要解释、不要加引号。
6. 没有依据时，用对方的含糊说法（如「啥呀」「没说过啊」）或反问一句；
   **绝对不要提「记录」「资料」「上下文」这类词**——真人不这么说话。

人设摘要：
- 关系：{relationship}
- 他怎么称呼对方：{addressing}
- 口头禅：{catchphrases}
- 常用字/词：{terms}
- 标点习惯：{punct}
- 绝对不要说的话：{forbidden}
- 示例回复：
{examples}
"""

    def _build_user_prompt(
        self,
        user_input: str,
        retrieved: List[HistoryHit],
        memory: ShortTermMemory,
        feedback: str = "",
    ) -> str:
        lines: List[str] = []
        if retrieved:
            lines.append("检索到的历史依据：")
            for hit in retrieved[:3]:
                ctx = f"（上文：{hit.context}）" if hit.context else ""
                lines.append(
                    f"- [{hit.speaker}] {hit.content}{ctx} score={hit.score:.2f}"
                )
        else:
            lines.append(
                "检索到的历史依据：无。若无法确认事实，用含糊短句或反问"
                "（不要说「记录里没提过」这类元话语）。"
            )

        recent = memory.to_list()[-4:] if hasattr(memory, "to_list") else []
        if recent:
            lines.append("本场最近对话：")
            for turn in recent:
                if isinstance(turn, dict):
                    lines.append(f"- 用户: {turn.get('user_input', '')}")
                    lines.append(f"- 你: {turn.get('assistant_reply', '')}")

        summary = getattr(memory, "summary", "")
        if summary:
            lines.append(f"早前对话摘要（窗口外，供你保持话题连贯）：{summary}")

        lines.append(f"用户刚说：{user_input}")
        if feedback:
            lines.append(f"{feedback}")
            lines.append("上面的问题必须修正，只输出改写后的回复正文：")
        else:
            lines.append("请用扮演角色的一句或几句短回复：")
        return "\n".join(lines)

    # -- transport -------------------------------------------------------
    def _payload(self, system: str, user: str, temperature: float, stream: bool) -> bytes:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": stream,
        }
        if stream:
            # 让服务端在最后一个 SSE 帧里带上 usage，否则流式拿不到 token 数。
            payload["stream_options"] = {"include_usage": True}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _report_usage(self, usage: dict, ttft_ms: Optional[float] = None) -> None:
        if usage:
            self.last_usage = dict(usage)
        if ttft_ms is not None:
            self.last_ttft_ms = ttft_ms
        if callable(self.on_usage):
            self.on_usage({"usage": dict(usage or {}), "ttft_ms": ttft_ms})

    def _open(self, data: bytes, timeout: Optional[float] = None):
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401:
                raise RuntimeError(
                    "鉴权失败（401）：检查 DEEPSEEK_API_KEY 是否有效、是否有多余空格。"
                ) from exc
            if exc.code == 402:
                raise RuntimeError(
                    "余额不足（402）：请到 platform.deepseek.com 充值。"
                ) from exc
            if exc.code == 429:
                raise RuntimeError("被限流（429）：降低并发或加重试退避。") from exc
            raise RuntimeError(f"模型接口 HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接模型接口 {self.base_url}：{exc.reason}") from exc

    def _chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        timeout: Optional[float] = None,
    ) -> str:
        with self._open(
            self._payload(system, user, temperature, stream=False), timeout=timeout
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
        self._report_usage(body.get("usage") or {})
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"模型返回格式异常：{body}") from exc

    def _chat_stream(
        self, system: str, user: str, temperature: float = 0.7
    ) -> Iterator[str]:
        """Parse OpenAI-style SSE: lines of `data: {...}` terminated by `data: [DONE]`."""

        started = time.perf_counter()
        first_delta_at: Optional[float] = None
        usage: dict = {}

        with self._open(self._payload(system, user, temperature, stream=True)) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    if first_delta_at is None:
                        first_delta_at = time.perf_counter()
                        self._report_usage(
                            {}, round((first_delta_at - started) * 1000, 1)
                        )
                    yield str(content)

        ttft = (
            round((first_delta_at - started) * 1000, 1)
            if first_delta_at is not None
            else None
        )
        self._report_usage(usage, ttft)


def chat_once(system: str, user: str, temperature: float = 0.0) -> str:
    """One-shot low-temperature call, reused by the evaluator's LLM judge.

    Judge 需要稳定的判定，所以默认 temperature=0；回复生成仍然用 0.7。
    """

    model = DeepSeekReplyModel()
    return model._chat(system=system, user=user, temperature=temperature)
