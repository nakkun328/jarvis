"""Memory importance: a documented 0-5 scale mapped onto the stored 0-1 score (JAR-28).

The design scale is 0 to 5; the stored ``importance`` stays 0 to 1 (schema and validation are
unchanged). The mapping is ``score = level / 5``, so a level is always recoverable from a stored
score by rounding. ``assess_importance`` is a deterministic, explainable rule set: it returns the
level, the score and the reasons that moved it. It never reads the network or a model, and it
never approves anything; the result is candidate metadata that a person can still edit in review.
"""

import re
from dataclasses import dataclass

from backend.memory.model import MemoryCategory, MemoryOrigin

MIN_LEVEL = 0
MAX_LEVEL = 5

#: What each level means. Shown to reviewers; the rules below are tuned to these anchors.
LEVEL_ANCHORS: dict[int, str] = {
    0: "noise: nothing worth keeping",
    1: "minor: harmless detail, may be forgotten",
    2: "useful: occasionally relevant context",
    3: "important: shapes ordinary answers about the user or a project",
    4: "high: the user stated it explicitly and it affects many answers",
    5: "critical: the user asked to remember it and it is a standing fact or rule",
}

_BASE_BY_CATEGORY = {
    MemoryCategory.USER: 3,
    MemoryCategory.PROJECT: 3,
    MemoryCategory.WORK_STATE: 2,
    MemoryCategory.SELF: 2,
    MemoryCategory.CONVERSATION: 1,
    MemoryCategory.TEMPORARY: 1,
}

_ORIGIN_DELTA = {
    MemoryOrigin.USER_EXPLICIT: 1,
    MemoryOrigin.CHAT: 0,
    MemoryOrigin.TOOL_OBSERVATION: 0,
    MemoryOrigin.RESEARCH: -1,
    MemoryOrigin.AI_INFERENCE: -1,
    MemoryOrigin.DISCUSSION: -2,
}

# Content that is not a verified fact about the user is capped however it was phrased.
_ORIGIN_CAP = {
    MemoryOrigin.AI_INFERENCE: 2,
    MemoryOrigin.DISCUSSION: 1,
    MemoryOrigin.RESEARCH: 3,
}

_REMEMBER = re.compile(r"覚えて|おぼえて|記憶して|忘れないで|remember (?:that|this)", re.IGNORECASE)
_STANDING = re.compile(
    r"いつも|常に|毎回|今後|これから|always|never|from now on|every time", re.IGNORECASE
)
_HEDGE = re.compile(
    r"たぶん|多分|かもしれ|と思う|おそらく|maybe|perhaps|probably|i think|might", re.IGNORECASE
)


@dataclass(frozen=True)
class ImportanceAssessment:
    level: int
    score: float
    reasons: tuple[str, ...]


def level_to_score(level: int) -> float:
    """Map a 0-5 level to the stored 0-1 score."""
    if isinstance(level, bool) or not isinstance(level, int) or not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError("importance level must be an integer from 0 to 5")
    return level / MAX_LEVEL


def score_to_level(score: float) -> int:
    """The nearest 0-5 level for a stored 0-1 score."""
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError("importance score must be between 0 and 1")
    return round(score * MAX_LEVEL)


def assess_importance(
    category: MemoryCategory, origin: MemoryOrigin, content: str
) -> ImportanceAssessment:
    """Rate one candidate. Deterministic; the same input always gives the same answer."""
    level = _BASE_BY_CATEGORY[category]
    reasons = [f"category:{category.value}={level}"]

    delta = _ORIGIN_DELTA[origin]
    if delta:
        level += delta
        reasons.append(f"origin:{origin.value}{delta:+d}")
    if _REMEMBER.search(content):
        level += 1
        reasons.append("asked-to-remember+1")
    if _STANDING.search(content):
        level += 1
        reasons.append("standing-rule+1")
    if _HEDGE.search(content):
        level -= 1
        reasons.append("hedged-1")

    cap = _ORIGIN_CAP.get(origin)
    if cap is not None and level > cap:
        level = cap
        reasons.append(f"cap:{origin.value}<={cap}")
    level = max(MIN_LEVEL, min(MAX_LEVEL, level))
    return ImportanceAssessment(level, level_to_score(level), tuple(reasons))
