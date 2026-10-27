"""FastAPI 适配器（可选依赖，预留）。

职责只有三件：路由、Pydantic 入参校验、把 handler 的 `(status, payload)` 写成响应。
业务逻辑全在 `transport.handlers` 里，所以这个文件是"薄"的——薄到可以整体替换成
`stdlib_adapter` 而行为不变（见 tests/test_transport.py 的同一套契约测试）。

没有 fastapi/pydantic 时：模块可以 import，只有 `create_app()` 抛 `DependencyMissing`。

注意：本模块**不能**加 `from __future__ import annotations`——Pydantic 模型定义在
`create_app()` 内部，路由参数注解必须在此刻求值成真实类型；一旦变成字符串，
FastAPI 会把它当成查询参数，全部请求变成 422（这个坑踩过一次，记在这里）。
"""

import os
from typing import Optional

from api.service import SessionService
from llm_client.factory import create_reply_model, load_dotenv
from transport.events import EventBus, NullSink
from transport.handlers import ApiHandlers, ValidationError, sse_frame
from transport.http.static import try_load


OPTIONAL_HINT = "pip install -r requirements-optional.txt"
DEFAULT_CHECKPOINT = os.environ.get("CHECKPOINT_PATH", "runtime/api.sqlite3")


class DependencyMissing(RuntimeError):
    """Raised when a reserved (optional) dependency is not installed yet."""


def http_stack_available() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(name) is not None
        for name in ("fastapi", "pydantic", "starlette")
    )


def _require_http_stack():
    try:
        from fastapi import FastAPI, HTTPException, Query, Response
        from fastapi.responses import JSONResponse, StreamingResponse
        from pydantic import BaseModel, Field
    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖就绪时不走这里
        raise DependencyMissing(
            f"缺少可选依赖 {exc.name!r}：HTTP 层属于预留能力，核心链路不需要它。\n"
            f"需要时统一安装：{OPTIONAL_HINT}"
        ) from exc
    return (
        FastAPI,
        HTTPException,
        Query,
        Response,
        JSONResponse,
        StreamingResponse,
        BaseModel,
        Field,
    )


def create_app(
    checkpoint_path: Optional[str] = None,
    model=None,
    max_steps: int = 12,
    service: Optional[SessionService] = None,
    bus: Optional[EventBus] = None,
):
    """Build the FastAPI app. Raises DependencyMissing when the stack is absent."""

    (
        FastAPI,
        HTTPException,
        Query,
        Response,
        JSONResponse,
        StreamingResponse,
        BaseModel,
        Field,
    ) = _require_http_stack()

    class ImportRequest(BaseModel):
        path: str = Field(..., description="本地聊天记录文件路径（UTF-8）")
        user_speaker: Optional[str] = None
        target_speaker: Optional[str] = None

    class SessionRequest(ImportRequest):
        session_id: Optional[str] = Field(
            None, description="可选；默认自动生成，等于 LangGraph 的 thread_id"
        )

    class MessageRequest(BaseModel):
        message: str = Field(..., min_length=1)

    class ResumeRequest(BaseModel):
        action: str = Field(..., pattern="^(approve|edit|reject)$")
        reply: Optional[str] = None

    load_dotenv()
    if service is None:
        service = SessionService(
            checkpoint_path or DEFAULT_CHECKPOINT,
            model=model
            if model is not None
            else create_reply_model(os.environ.get("PROVIDER", "auto")),
            max_steps=max_steps,
        )
    handlers = ApiHandlers(service, bus or EventBus([NullSink()]))

    app = FastAPI(
        title="Persona Replay API",
        description="双人聊天语气分身：导入 → 建会话 → 流式扮演 → 人工裁决",
        version="0.9.0",
    )
    app.state.service = service
    app.state.handlers = handlers

    # 鉴权 + 限流放在中间件里：一处生效，新增路由不会漏。
    @app.middleware("http")
    async def _security_middleware(request, call_next):
        client = request.client.host if request.client else ""
        denied = handlers.guard(request.headers, client, request.url.path)
        if denied is not None:
            status, payload = denied
            response = JSONResponse(status_code=status, content=payload)
            retry_after = payload.get("retry_after_seconds")
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response
        return await call_next(request)

    @app.exception_handler(ValidationError)
    def _validation_error(request, exc: ValidationError):  # pragma: no cover
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        status, payload = handlers.health()
        return JSONResponse(status_code=status, content=payload)

    @app.post("/chats/import")
    def import_chat(request: ImportRequest):
        status, payload = handlers.import_chat(request.model_dump())
        return JSONResponse(status_code=status, content=payload)

    @app.post("/sessions")
    def create_session(request: SessionRequest):
        status, payload = handlers.create_session(request.model_dump())
        return JSONResponse(status_code=status, content=payload)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str):
        status, payload = handlers.get_session(session_id)
        return JSONResponse(status_code=status, content=payload)

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str):
        status, payload = handlers.delete_session(session_id)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, request: MessageRequest):
        status, payload = handlers.post_message(session_id, request.model_dump())
        return JSONResponse(status_code=status, content=payload)

    @app.post("/sessions/{session_id}/resume")
    def resume(session_id: str, request: ResumeRequest):
        status, payload = handlers.resume(session_id, request.model_dump())
        return JSONResponse(status_code=status, content=payload)

    @app.get("/sessions/{session_id}/stream")
    def stream(
        session_id: str,
        message: str = Query(..., min_length=1, description="用户这一轮说的话"),
    ):
        status, body = handlers.stream_events(session_id, {"message": message})
        if status != 200:
            return JSONResponse(status_code=status, content=body)
        return StreamingResponse(
            (sse_frame(event, data) for event, data in body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 静态资源必须放在最后：catch-all 路由会吃掉它后面声明的所有 GET。
    @app.get("/")
    def index():
        loaded = try_load("/")
        if loaded is None:
            raise HTTPException(status_code=404, detail="web/index.html missing")
        content_type, body = loaded
        return Response(content=body, media_type=content_type)

    @app.get("/{asset_path:path}")
    def static_asset(asset_path: str):
        loaded = try_load("/" + asset_path)
        if loaded is None:
            raise HTTPException(status_code=404, detail="not found")
        content_type, body = loaded
        return Response(content=body, media_type=content_type)

    return app
