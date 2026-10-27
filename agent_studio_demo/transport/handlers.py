"""纯业务处理器：不依赖任何 HTTP 框架。

所有适配器（FastAPI / 标准库）只做三件事：路由、解析 JSON、把 `(status, payload)`
写成响应。这样：

- 业务逻辑可以用普通单测覆盖，不需要起服务器；
- 换协议（HTTP → 别的东西）不用碰业务；
- 可选依赖缺失时，业务层照常工作。

错误约定（两个适配器一致）：
    422  入参不合法（缺字段 / 空消息 / 非法 action）
    400  领域校验失败（文件不存在、说话人非法）
    404  会话不存在
    409  状态冲突（对未中断的会话调 resume）
"""

from __future__ import annotations

import hmac
import json
import math
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from api.service import SessionNotFound, SessionService, persona_for_chat, session_payload
from transport.events import EventBus


API_VERSION = "0.11.0"
SSE_TIMEOUT_SECONDS = 120
REPLY_CHUNK_SIZE = 2
VALID_ACTIONS = ("approve", "edit", "reject")
# 这些路径不需要鉴权：健康检查给探活/负载均衡用，静态资源给浏览器加载页面。
PUBLIC_PATHS = ("/health", "/", "/index.html", "/app.js", "/style.css")
STATIC_SUFFIXES = (".html", ".js", ".css", ".ico", ".png", ".svg", ".woff2", ".map")


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    lowered = (path or "").lower()
    return lowered.endswith(STATIC_SUFFIXES)

# 只允许导入这个目录下的聊天文件。前端把 path 暴露给浏览器之后，这不再是"可选项"：
# 没有它，`{"path": "C:/Users/.../任何文件"}` 就能读到进程能读的任何东西。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAT_ROOT = Path(os.environ.get("CHAT_ROOT") or PROJECT_ROOT).resolve()

Status = int
Payload = Dict[str, Any]
Event = Tuple[str, Dict[str, Any]]


class ValidationError(ValueError):
    """Raised when a request body is malformed (mapped to 422)."""


class ForbiddenPath(ValueError):
    """Raised when a chat path escapes CHAT_ROOT (mapped to 400/403)."""


def _safe_chat_path(raw: str) -> str:
    """Resolve a chat path and refuse anything outside CHAT_ROOT."""

    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else CHAT_ROOT / candidate).resolve()
    if resolved != CHAT_ROOT and CHAT_ROOT not in resolved.parents:
        raise ForbiddenPath(
            f"path outside CHAT_ROOT ({CHAT_ROOT}): {raw!r}"
        )
    return str(resolved)


def stream_enabled() -> bool:
    """`STREAM=1` 时把模型增量直接推给客户端（远程模型的首 token 延迟靠它）。

    默认关闭：非流式时 `reply_delta` 是回合结束后的回放，事件顺序
    `retrieved → draft → critique → reply_delta → done` 更稳定；
    开启后顺序变成 `retrieved → draft → reply_delta* → critique → done`
    （正文先于判定出现，这是流式的必然代价）。
    """

    return os.environ.get("STREAM", "0").strip().lower() not in ("", "0", "false", "no")


def _require_str(payload: dict, key: str, allow_empty: bool = False) -> str:
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")
    value = payload.get(key)
    if value is None:
        raise ValidationError(f"missing field: {key}")
    if not isinstance(value, str):
        raise ValidationError(f"field {key!r} must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"field {key!r} must not be empty")
    return value


def _optional_str(payload: dict, key: str) -> Optional[str]:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"field {key!r} must be a string")
    return value or None


def sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def node_events(name: str, state: dict) -> List[Event]:
    """Map a graph event to wire events (transport-agnostic)."""

    if name == "planner":
        # persona 在流开始时统一发一次，这里不重复。
        return []
    if name == "retrieve":
        return [("retrieved", {"hits": state.get("retrieved", [])})]
    if name == "draft":
        return [("draft", {"draft": state.get("draft", "")})]
    if name == "critic":
        return [("critique", {"critique": state.get("critique")})]
    if name == "interrupt":
        return [("interrupt", state.get("pending") or {})]
    return []


def chunks(text: str, size: int = REPLY_CHUNK_SIZE) -> List[str]:
    if not text:
        return []
    return [text[index : index + size] for index in range(0, len(text), size)]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _bearer_token(headers) -> str:
    """从 Authorization 头取 token；也接受直接放在头里的裸 token。"""

    value = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter("Authorization")
    if value is None and hasattr(headers, "items"):
        for key, item in headers.items():
            if str(key).lower() == "authorization":
                value = item
                break
    if not value:
        return ""
    parts = str(value).split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return str(value).strip()


class ApiHandlers:
    def __init__(
        self,
        service: SessionService,
        bus: Optional[EventBus] = None,
        token: Optional[str] = None,
        rate_limit_per_minute: Optional[int] = None,
        clock=time.monotonic,
    ):
        self.service = service
        self.bus = bus
        # 没配 token = 不鉴权（默认只监听 127.0.0.1，外面进不来）。
        self.token = (token if token is not None else os.environ.get("API_TOKEN", "")).strip()
        if rate_limit_per_minute is None:
            rate_limit_per_minute = _env_int("RATE_LIMIT_PER_MINUTE", 0)
        self.rate_limit = max(0, int(rate_limit_per_minute))
        self._clock = clock
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._bucket_lock = threading.Lock()
        self.rejected_auth = 0
        self.rejected_rate = 0

    # -- 安全闸门 --------------------------------------------------------
    def guard(
        self, headers: Optional[dict], client: str = "", path: str = ""
    ) -> Optional[Tuple[Status, Payload]]:
        """返回 None 表示放行；否则返回要直接写回的 (status, payload)。"""

        if _is_public(path):
            return None

        if self.token:
            supplied = _bearer_token(headers or {})
            if not supplied or not hmac.compare_digest(supplied, self.token):
                self.rejected_auth += 1
                return 401, {
                    "detail": "unauthorized：需要在 Authorization: Bearer <token> 里带 API_TOKEN",
                }

        if self.rate_limit > 0:
            allowed, retry_after = self._consume(client or "anonymous")
            if not allowed:
                self.rejected_rate += 1
                return 429, {
                    "detail": "rate limited",
                    "retry_after_seconds": retry_after,
                }
        return None

    def _consume(self, key: str) -> Tuple[bool, int]:
        """令牌桶：容量 = 每分钟额度，按秒匀速回填。"""

        capacity = float(self.rate_limit)
        refill_per_second = capacity / 60.0
        now = self._clock()
        with self._bucket_lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * refill_per_second)
            if tokens < 1.0:
                wait = math.ceil((1.0 - tokens) / refill_per_second) if refill_per_second else 60
                self._buckets[key] = (tokens, now)
                return False, max(1, wait)
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0

    def security_summary(self) -> dict:
        return {
            "auth": "bearer" if self.token else "off",
            "rate_limit_per_minute": self.rate_limit,
            "rejected_auth": self.rejected_auth,
            "rejected_rate": self.rejected_rate,
        }

    # -- 基础 ------------------------------------------------------------
    def health(self) -> Tuple[Status, Payload]:
        payload: Payload = {"status": "ok", "version": API_VERSION}
        model_name = getattr(self.service, "model_name", "")
        if model_name:
            payload["model"] = model_name
        payload["stream"] = stream_enabled()
        payload["auth_required"] = bool(self.token)
        return 200, payload

    # -- 导入与会话 ------------------------------------------------------
    def import_chat(self, payload: dict) -> Tuple[Status, Payload]:
        try:
            path = _safe_chat_path(_require_str(payload, "path"))
        except ForbiddenPath as exc:
            return 400, {"detail": str(exc)}
        try:
            session = self.service.import_chat(
                path,
                _optional_str(payload, "user_speaker"),
                _optional_str(payload, "target_speaker"),
            )
        except ValueError as exc:
            return 400, {"detail": str(exc)}
        return 200, {
            "chat_id": session.chat_id,
            "speakers": session.speakers,
            "user_speaker": session.user_speaker,
            "target_speaker": session.target_speaker,
            "message_count": len(session.chat.messages),
            "persona": persona_for_chat(session),
        }

    def create_session(self, payload: dict) -> Tuple[Status, Payload]:
        try:
            path = _safe_chat_path(_require_str(payload, "path"))
        except ForbiddenPath as exc:
            return 400, {"detail": str(exc)}
        try:
            state = self.service.create_session(
                path,
                _optional_str(payload, "user_speaker"),
                _optional_str(payload, "target_speaker"),
                _optional_str(payload, "session_id"),
            )
        except ValueError as exc:
            return 400, {"detail": str(exc)}
        return 201, session_payload(state)

    def get_session(self, session_id: str) -> Tuple[Status, Payload]:
        try:
            return 200, session_payload(self.service.get(session_id))
        except SessionNotFound:
            return 404, {"detail": "session not found"}

    def delete_session(self, session_id: str) -> Tuple[Status, Payload]:
        try:
            self.service.delete(session_id)
        except SessionNotFound:
            return 404, {"detail": "session not found"}
        return 200, {"deleted": session_id}

    # -- 一轮扮演 --------------------------------------------------------
    def post_message(self, session_id: str, payload: dict) -> Tuple[Status, Payload]:
        message = _require_str(payload, "message")
        try:
            state = self.service.send(session_id, message, on_event=self.bus)
        except SessionNotFound:
            return 404, {"detail": "session not found"}
        except ValueError as exc:
            return 400, {"detail": str(exc)}
        return 200, session_payload(state)

    def resume(self, session_id: str, payload: dict) -> Tuple[Status, Payload]:
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        action = str(payload.get("action") or "").strip().lower()
        if action not in VALID_ACTIONS:
            raise ValidationError(
                f"action must be one of {list(VALID_ACTIONS)}, got {action!r}"
            )
        reply = payload.get("reply")
        if reply is not None and not isinstance(reply, str):
            raise ValidationError("field 'reply' must be a string")
        try:
            state = self.service.resume(
                session_id,
                {"action": action, "reply": reply or ""},
                on_event=self.bus,
            )
        except SessionNotFound:
            return 404, {"detail": "session not found"}
        except ValueError as exc:
            return 409, {"detail": str(exc)}
        return 200, session_payload(state)

    # -- SSE -------------------------------------------------------------
    def stream_events(
        self, session_id: str, query: dict
    ) -> Tuple[Status, Any]:
        """Return (status, iterator) so 404 can be sent before the body starts."""

        message = _require_str(query, "message")
        try:
            current = self.service.get(session_id)
        except SessionNotFound:
            return 404, {"detail": "session not found"}
        return 200, self._event_iter(current, session_id, message)

    def _event_iter(self, current, session_id: str, message: str) -> Iterator[Event]:
        yield (
            "persona",
            {
                "persona": current.persona,
                "speakers": current.speakers,
                "user_speaker": current.user_speaker,
                "target_speaker": current.target_speaker,
            },
        )

        channel: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        streaming = stream_enabled()
        sent_deltas = False

        def emit(name: str, state: dict) -> None:
            channel.put((name, state))

        def delta(text: str, reset: bool = False) -> None:
            channel.put(("__delta__", {"delta": text, "reset": bool(reset)}))

        def worker() -> None:
            try:
                final = self.service.send(
                    session_id,
                    message,
                    on_event=emit,
                    on_delta=delta if streaming else None,
                )
                channel.put(("__final__", {"state": final.to_dict()}))
            except SessionNotFound:
                channel.put(("__error__", {"detail": "session not found", "code": 404}))
            except ValueError as exc:
                channel.put(("__error__", {"detail": str(exc), "code": 400}))
            except Exception as exc:  # noqa: BLE001 - 流里的异常必须变成事件
                channel.put(("__error__", {"detail": str(exc), "code": 500}))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            try:
                name, payload = channel.get(timeout=SSE_TIMEOUT_SECONDS)
            except queue.Empty:
                yield ("error", {"detail": "turn timed out", "code": 504})
                return

            if name == "__error__":
                yield ("error", payload)
                return

            if name == "__delta__":
                sent_deltas = True
                if payload.get("reset"):
                    # 重写：让客户端清空上一版正文。
                    yield ("reply_reset", {"reason": "critic_rewrite"})
                elif payload.get("delta"):
                    yield ("reply_delta", {"delta": payload["delta"]})
                continue

            if name == "__final__":
                state = payload["state"]
                status = state.get("status", "ok")
                if status != "interrupted" and not sent_deltas:
                    for chunk in chunks(str(state.get("reply", ""))):
                        yield ("reply_delta", {"delta": chunk})
                yield (
                    "done",
                    {
                        "status": status,
                        "reply": state.get("reply", ""),
                        "turn_count": state.get("turn_count", 0),
                        "rewrite_count": state.get("rewrite_count", 0),
                        "steps": state.get("steps", []),
                    },
                )
                return

            for event in node_events(name, payload):
                yield event
