"""Day 1 chat fixture parser for the Persona Replay demo.

Supported input forms:
- ``speaker: message`` / ``speaker：message``
- ``[YYYY-MM-DD HH:MM] speaker: message``
- ``YYYY-MM-DD HH:MM:SS speaker`` followed by a message line
- Continuation lines after any parsed message
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


_TIMESTAMP = r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
_INLINE_MESSAGE = re.compile(
    rf"^\s*(?:\[(?P<bracket_timestamp>{_TIMESTAMP})\]\s*)?"
    rf"(?:(?P<plain_timestamp>{_TIMESTAMP})\s+)?"
    rf"(?P<speaker>[^:：\t\n]{{1,40}}?)\s*[:：]\s*(?P<content>.+?)\s*$"
)
_TIMESTAMP_HEADER = re.compile(
    rf"^\s*(?:\[(?P<bracket_timestamp>{_TIMESTAMP})\]|"
    rf"(?P<plain_timestamp>{_TIMESTAMP}))\s+"
    rf"(?P<speaker>[^:：\t\n]{{1,40}})\s*$"
)


@dataclass
class ChatMessage:
    """One normalized chat message."""

    speaker: str
    content: str
    line_number: int
    timestamp: Optional[str] = None


@dataclass
class ParsedChat:
    """Parsed conversation with stable speaker order."""

    messages: List[ChatMessage]

    @property
    def speakers(self) -> Tuple[str, ...]:
        """Return unique speakers in the order they first appear."""

        return tuple(dict.fromkeys(message.speaker for message in self.messages))

    @property
    def speaker_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for message in self.messages:
            counts[message.speaker] = counts.get(message.speaker, 0) + 1
        return counts

    def require_two_speakers(self) -> Tuple[str, str]:
        """Validate the Day1 product constraint: exactly two speakers."""

        speakers = self.speakers
        if len(speakers) != 2:
            raise ValueError(
                f"expected exactly two speakers, found {len(speakers)}: {list(speakers)}"
            )
        return speakers[0], speakers[1]


def _timestamp_from_match(match: re.Match[str]) -> Optional[str]:
    return match.group("bracket_timestamp") or match.group("plain_timestamp")


def parse_chat_text(text: str) -> ParsedChat:
    """Parse UTF-8 chat text into normalized messages.

    A timestamp-only speaker line is supported for common export formats where
    the message body is placed on the following line. Non-empty lines that do
    not start a new message are treated as continuations of the previous one.
    """

    messages: List[ChatMessage] = []
    current: Optional[ChatMessage] = None
    pending_header: Optional[Tuple[str, Optional[str], int]] = None

    for line_number, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        # Check timestamp-only headers before inline messages because the
        # timestamp itself contains colons and could otherwise look like a
        # malformed ``speaker: content`` line.
        header_match = _TIMESTAMP_HEADER.match(raw_line)
        if header_match:
            if current is not None:
                messages.append(current)
            pending_header = (
                header_match.group("speaker").strip(),
                _timestamp_from_match(header_match),
                line_number,
            )
            current = None
            continue

        inline_match = _INLINE_MESSAGE.match(raw_line)
        if inline_match:
            if current is not None:
                messages.append(current)
            current = ChatMessage(
                speaker=inline_match.group("speaker").strip(),
                content=inline_match.group("content").strip(),
                timestamp=_timestamp_from_match(inline_match),
                line_number=line_number,
            )
            pending_header = None
            continue

        if pending_header is not None:
            speaker, timestamp, header_line_number = pending_header
            current = ChatMessage(
                speaker=speaker,
                content=line,
                timestamp=timestamp,
                line_number=header_line_number,
            )
            pending_header = None
            continue

        if current is None:
            raise ValueError(f"cannot parse line {line_number}: {raw_line!r}")
        current.content = f"{current.content}\n{line}"

    if current is not None:
        messages.append(current)
    if pending_header is not None:
        raise ValueError(f"timestamp header at line {pending_header[2]} has no message")
    if not messages:
        raise ValueError("chat input contains no messages")

    return ParsedChat(messages=messages)


def parse_chat_file(path: str | Path) -> ParsedChat:
    """Read and parse a UTF-8 chat export from disk."""

    chat_path = Path(path)
    return parse_chat_text(chat_path.read_text(encoding="utf-8-sig"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a two-person chat fixture")
    parser.add_argument("path", type=Path, help="UTF-8 chat export path")
    args = parser.parse_args(argv)

    parsed = parse_chat_file(args.path)
    first, second = parsed.require_two_speakers()
    print(f"speakers: {first}, {second}")
    print(f"messages: {len(parsed.messages)}")
    print(f"counts: {parsed.speaker_counts}")
    for message in parsed.messages:
        prefix = f"[{message.timestamp}] " if message.timestamp else ""
        print(f"{prefix}{message.speaker}: {message.content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
