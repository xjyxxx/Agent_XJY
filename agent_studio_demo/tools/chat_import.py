"""多格式聊天导入：txt / csv / html。

为什么单独一个模块：`tools/chat_parser.py` 只认 `姓名: 内容` 这种手写格式，
而真实导出（微信/QQ/第三方工具）常见 CSV 和 HTML。把"格式识别"和"文本解析"
分开，主链路就能统一拿到 `ParsedChat`。

支持的形状（都是实测过的、常见导出结构）：

1. `.txt` —— 交给 `chat_parser`（`姓名: 内容`、时间戳头、续行）
2. `.csv` —— 自动嗅探分隔符；表头含 时间/发送者/内容 之类就走命名列，
             否则按 3 列（时间, 说话人, 内容）或 2 列（说话人, 内容）兜底
3. `.html` —— 先按常见 class（nickname/name + content/text）配对抽取；
             抽不到就剥标签退化成文本解析

诚实边界：导出工具五花八门，这里只覆盖"能拿到每行说话人+内容"的结构；
拿不到的（图片/表情/引用块）会被跳过，不猜。
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from tools.chat_parser import ChatMessage, ParsedChat, parse_chat_file, parse_chat_text

SUPPORTED_SUFFIXES = (".txt", ".log", ".csv", ".tsv", ".html", ".htm", ".jsonl")

_NAME_HINTS = ("发送者", "昵称", "说话人", "sender", "name", "from", "user", "nickname")
_CONTENT_HINTS = ("内容", "消息", "message", "content", "text", "body")
_TIME_HINTS = ("时间", "time", "date", "timestamp", "日期")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")


class UnsupportedFormat(ValueError):
    """Raised when a file is not a shape we can import."""


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = _TAG_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _pick(header: Sequence[str], hints: Sequence[str]) -> Optional[int]:
    lowered = [item.strip().lower() for item in header]
    for hint in hints:
        for index, name in enumerate(lowered):
            if hint in name:
                return index
    return None


def parse_csv(path: str | Path) -> ParsedChat:
    """Parse a WeChat/QQ style CSV export."""

    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8-sig", errors="replace")
    # 嗅探分隔符：逗号 / 制表符 / 分号
    sample = raw[:2000]
    delimiter = max((",", "\t", ";"), key=sample.count)
    rows = [row for row in csv.reader(raw.splitlines(), delimiter=delimiter) if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"csv 没有内容: {file_path}")

    header = rows[0]
    named = (
        _pick(header, _TIME_HINTS),
        _pick(header, _NAME_HINTS),
        _pick(header, _CONTENT_HINTS),
    )
    if named[1] is not None and named[2] is not None:
        body = rows[1:]
        time_index, name_index, content_index = named
    else:
        body = rows
        time_index = None
        name_index, content_index = (0, 1) if len(header) == 2 else (1, 2)

    messages: List[ChatMessage] = []
    for line_number, row in enumerate(body, start=2):
        if max(name_index, content_index) >= len(row):
            continue
        speaker = _clean(row[name_index])
        content = _clean(row[content_index])
        if not speaker or not content:
            continue
        timestamp = None
        if time_index is not None and time_index < len(row):
            timestamp = _clean(row[time_index]) or None
        messages.append(
            ChatMessage(
                speaker=speaker,
                content=content,
                line_number=line_number,
                timestamp=timestamp,
            )
        )
    if not messages:
        raise ValueError(f"csv 里没有解析出任何消息: {file_path}")
    return ParsedChat(messages=messages)


_NAME_BLOCK = re.compile(
    r'<(?P<tag>\w+)[^>]*class="[^"]*(?:nickname|username|sender|name)[^"]*"[^>]*>(?P<name>.*?)</(?P=tag)>',
    re.IGNORECASE | re.DOTALL,
)
# 精确优先：content/text/bubble 是"正文"的常见类名。
_CONTENT_BLOCK = re.compile(
    r'<(?P<tag>\w+)[^>]*class="[^"]*(?:content|text|bubble)[^"]*"[^>]*>(?P<body>.*?)</(?P=tag)>',
    re.IGNORECASE | re.DOTALL,
)
# 兜底：有些导出把正文放在 class="message" 里——但它也可能是个包裹层，
# 所以只在精确匹配为空时才用，并且要剔除里面混进的昵称文本。
_CONTENT_GENERIC = re.compile(
    r'<(?P<tag>\w+)[^>]*class="[^"]*(?:message|msg)[^"]*"[^>]*>(?P<body>.*?)</(?P=tag)>',
    re.IGNORECASE | re.DOTALL,
)


def parse_html(path: str | Path) -> ParsedChat:
    """Parse a chat export HTML: nickname/content blocks, else fall back to text."""

    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8", errors="replace")

    names = [(_clean(match.group("name")), match.start()) for match in _NAME_BLOCK.finditer(raw)]
    bodies = [(_clean(match.group("body")), match.start()) for match in _CONTENT_BLOCK.finditer(raw)]
    if not bodies:
        bodies = [
            (_clean(match.group("body")), match.start())
            for match in _CONTENT_GENERIC.finditer(raw)
        ]

    messages: List[ChatMessage] = []
    if names and len(bodies) >= len(names):
        # 按出现顺序配对：第 i 个名字对应第 i 段内容
        for index, (speaker, position) in enumerate(names):
            content = bodies[index][0] if index < len(bodies) else ""
            # 宽松匹配可能把昵称文本一起带进来，去掉前缀
            if content.startswith(speaker):
                content = content[len(speaker):].strip()
            if not speaker or not content:
                continue
            messages.append(
                ChatMessage(
                    speaker=speaker,
                    content=content,
                    line_number=index + 1,
                )
            )

    if not messages:
        # 退化路径：剥标签后当作纯文本，要求原文本身有 "A: hi" 这样的行
        plain = _clean(raw.replace("</div>", "\n").replace("</p>", "\n").replace("<br/>", "\n").replace("<br>", "\n"))
        messages = parse_chat_text(plain).messages

    if not messages:
        raise ValueError(
            f"html 里没有识别出消息: {file_path}。"
            "支持的结构：class 含 nickname/name 的说话人 + class 含 content/text 的内容"
        )
    return ParsedChat(messages=messages)


def parse_jsonl(path: str | Path) -> ParsedChat:
    """Each line: {"speaker": "...", "content": "...", "timestamp": "..."}."""

    file_path = Path(path)
    messages: List[ChatMessage] = []
    for line_number, line in enumerate(
        file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON: {exc}") from exc
        speaker = str(item.get("speaker") or item.get("name") or "").strip()
        content = str(item.get("content") or item.get("text") or "").strip()
        if not speaker or not content:
            continue
        messages.append(
            ChatMessage(
                speaker=speaker,
                content=content,
                line_number=line_number,
                timestamp=item.get("timestamp"),
            )
        )
    if not messages:
        raise ValueError(f"jsonl 里没有消息: {file_path}")
    return ParsedChat(messages=messages)


def import_chat(path: str | Path) -> ParsedChat:
    """Dispatch by suffix; always returns a ParsedChat (speakers validated later)."""

    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"聊天文件不存在: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix == ".csv" or suffix == ".tsv":
        return parse_csv(file_path)
    if suffix in (".html", ".htm"):
        return parse_html(file_path)
    if suffix == ".jsonl":
        return parse_jsonl(file_path)
    if suffix in (".txt", ".log"):
        return parse_chat_file(file_path)
    raise UnsupportedFormat(
        f"不支持的格式 {suffix!r}；支持: {', '.join(SUPPORTED_SUFFIXES)}"
    )


def supported_formats() -> List[Tuple[str, str]]:
    return [
        (".txt", "姓名: 内容（支持时间戳头与多行续写）"),
        (".csv", "时间,说话人,内容（列名自动识别；2/3 列均可）"),
        (".html", "class 含 nickname/name + content/text 的消息块"),
        (".jsonl", '每行 {"speaker","content","timestamp"}'),
    ]
