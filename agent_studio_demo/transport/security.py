"""对外暴露时的安全底线：非回环地址 + 没配 token = 拒绝启动。

为什么做成"启动即拒绝"：默认监听 127.0.0.1 时，只有本机能访问，不鉴权是合理的；
一旦 `HOST=0.0.0.0`（Docker、局域网演示），HTTP 层就是完全开放的——任何能访问
端口的人都能花你的 key、读你的聊天记录。这种错误不该等到"线上被人刷了"才发现。
"""

from __future__ import annotations

LOOPBACK_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "0:0:0:0:0:0:0:1",
    "127.0.0.01",
}


class InsecureBind(RuntimeError):
    """监听公网地址但没有配置 API_TOKEN。"""


def is_loopback(host: str | None) -> bool:
    value = (host or "").strip().lower()
    if not value:
        return False
    return value in LOOPBACK_HOSTS or value.startswith("127.")


def ensure_bind_is_safe(host: str | None, token: str | None) -> None:
    """非回环 + 无 token → 抛 InsecureBind。"""

    if is_loopback(host):
        return
    if (token or "").strip():
        return
    raise InsecureBind(
        f"拒绝启动：HOST={host} 会对外暴露，但没有配置 API_TOKEN。\n"
        "两种改法（二选一）：\n"
        "  1) 只让本机访问：$env:HOST = '127.0.0.1'\n"
        "  2) 对外提供服务：在 config/settings.ini 的 [server] 里填 api_token = <一串随机字符>\n"
        "     客户端需要带 Authorization: Bearer <token>\n"
        "（另外建议同时设 rate_limit_per_minute，避免 key 被刷）"
    )


__all__ = ["InsecureBind", "ensure_bind_is_safe", "is_loopback"]
