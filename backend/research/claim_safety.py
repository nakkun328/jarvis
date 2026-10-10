"""Conservative checks that decide whether a research claim may be approved automatically.

A quote-verified claim only proves that the quote exists on the page; the page itself is
untrusted web text. Before the owner-approved automatic approval acts, a claim must pass both
checks here. A claim that fails stays ``pending`` for a person to review, exactly as without
the automatic approval. The thresholds live in this one place.
"""

import re
import unicodedata

from backend.research.models import ResearchSource, SourceType

#: A source is weak when its type is one of WEAK_SOURCE_TYPES AND its authority rating is
#: missing or below MIN_AUTHORITY. Official/docs/academic/news sources are never weak by this
#: rule (see AUTHORITY_BY_TYPE in backend/research/evaluation.py: blog .3, forum .35,
#: community .4, unknown .25, news .6).
MIN_AUTHORITY = 0.5
WEAK_SOURCE_TYPES = frozenset(
    {SourceType.UNKNOWN, SourceType.BLOG, SourceType.COMMUNITY, SourceType.FORUM}
)

_INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule|direction)s?\b",
        r"\b(previous|prior|above|earlier|system|hidden|new)\s+(instruction|prompt|message)s?\b",
        r"\bsystem\s*prompt\b",
        r"\b(you|assistant|ai|model|llm|chatbot)\s+(must|should|shall|need to|have to|will)\b",
        r"\b(you are|act as|pretend to be|from now on|as an ai)\b",
        r"\b(do not|don't|never)\s+(tell|inform|mention|reveal|show)\b",
        r"\b(reveal|print|output|repeat)\b.{0,30}\b(prompt|instruction|secret|key|password)s?\b",
        r"\b(tool|function)[ _-]?calls?\b",
        r"(^|\n)\s*(system|assistant|user|developer)\s*[:：]",
        r"</?\s*(system|assistant|user|instruction|prompt|tool)\b",
        r"<\|[^|>]*\|>|\[/?(inst|sys)\]|```",
        r"指示(を|に)?(無視|従|変更|上書き)",
        r"(これまで|以前|上記|前)の(指示|命令|プロンプト)",
        r"(システム|隠し)?プロンプト",
        r"(命令|指示)(です|する|します|に従)",
        r"あなたは.{0,30}(として|のように)(振る舞|ふるま|答え|応答|行動)",
        r"(ユーザー|利用者|人間)(に|には)(は)?(言わ|伝え|知らせ|教え)ない",
        r"(してください|しなさい|すること|せよ)[。.!！]*\s*$",
    )
)


def is_weak_source(source: ResearchSource) -> bool:
    authority = source.evaluation.authority
    return source.source_type in WEAK_SOURCE_TYPES and (
        authority is None or authority < MIN_AUTHORITY
    )


def looks_like_instruction(*texts: str) -> bool:
    """Whether any text reads like an instruction aimed at a model (deterministic, best effort)."""
    for text in texts:
        normalized = unicodedata.normalize("NFKC", text)
        for line in (normalized, *normalized.splitlines()):
            if any(pattern.search(line) for pattern in _INSTRUCTION_PATTERNS):
                return True
    return False


def auto_approvable(source: ResearchSource, claim_text: str, quote: str) -> bool:
    """Both checks: not a weak source, and neither claim nor quote reads like an instruction."""
    return not is_weak_source(source) and not looks_like_instruction(
        claim_text, quote, source.title or "", source.publisher or ""
    )
