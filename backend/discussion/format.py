"""How a discussion message is stored in a conversation (docs/discussion.md).

The message table has no speaker column and the schema is not changed, so the speaker is carried
in the text as a fixed prefix: ``【名前】`` for a persona, ``【まとめ】`` for the moderator's
summary and ``【お知らせ】`` for a notice. A message without the prefix has no speaker (``None``):
the user's own topic, or any ordinary chat message. These helpers are the only place that knows the
format, so a later migration can move the speaker into a column by changing them alone.
"""

import re
from dataclasses import dataclass

SUMMARY_SPEAKER = "まとめ"
NOTICE_SPEAKER = "お知らせ"
#: Names a persona must not use: they would be indistinguishable from the fixed speakers.
RESERVED_SPEAKERS = frozenset({SUMMARY_SPEAKER, NOTICE_SPEAKER, "まとめ役", "システム"})

_PREFIX = re.compile(r"【([^【】\n]{1,20})】")
_NOTICE_CODE = re.compile(r"（([A-Z][A-Z_]{1,40})）\Z")


@dataclass(frozen=True)
class ParsedMessage:
    speaker: str | None
    text: str


def format_message(speaker: str, text: str) -> str:
    """``【speaker】text``. The speaker is a validated persona name or a fixed speaker."""
    return f"【{speaker}】{text}"


def format_summary(text: str) -> str:
    return format_message(SUMMARY_SPEAKER, text)


def format_notice(text: str, code: str) -> str:
    """A notice carries its fixed code at the end, in full-width parentheses."""
    return format_message(NOTICE_SPEAKER, f"{text}（{code}）")


def parse_message(content: str) -> ParsedMessage:
    """Split a stored message into speaker and text. No prefix: the speaker is ``None``."""
    match = _PREFIX.match(content) if isinstance(content, str) else None
    if match is None:
        return ParsedMessage(None, content if isinstance(content, str) else "")
    return ParsedMessage(match.group(1), content[match.end() :])


def notice_code(text: str) -> str | None:
    """The fixed code at the end of a notice's text, or None."""
    match = _NOTICE_CODE.search(text)
    return match.group(1) if match else None
