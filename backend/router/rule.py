"""A transparent keyword baseline router.

This is a BASELINE for tests and an offline fallback. It is NOT the intended
method: the real router reads paraphrases and context with a model (see
``LLMRouter``). Fixed keywords miss paraphrases by design, and the evaluation
set contains such cases so the gap stays visible.
"""

import re

from backend.router.contract import (
    DEFAULT_MAX_INPUT_CHARS,
    Route,
    RouteDecision,
    RouteReason,
    fallback,
    precheck_input,
    validate_max_chars,
)

RULE_CONFIDENCE = 0.8
CONFLICT_CONFIDENCE = 0.5

# Each rule is (route, pattern). Matching is on the whole text, case-insensitive.
RULES: tuple[tuple[Route, re.Pattern[str]], ...] = (
    (
        Route.memory,
        re.compile(
            r"前に話|以前話|前回の|先週(?:話|相談)|さっきの|覚えて|覚えている"
            r"|私の(?:好|趣味|誕生日|名前|目標|家族|アレルギー)|昨日話|いつもの"
        ),
    ),
    (
        Route.research,
        re.compile(
            r"最新|ニュース|調べて|検索|比較|比べて|ランキング|為替|今日の.{0,6}天気"
            r"|search for|latest news",
            re.IGNORECASE,
        ),
    ),
    (
        Route.casual,
        re.compile(
            r"^(?:こんにちは|おはよう|こんばんは|おやすみ|ただいま|お疲れ|ありがとう)"
            r"|疲れた|眠い|退屈|雑談|やったー|寂しい"
        ),
    ),
)


class RuleRouter:
    """Keyword/pattern baseline. Conflicting or absent matches fall back safely."""

    def __init__(self, *, max_input_chars: int = DEFAULT_MAX_INPUT_CHARS) -> None:
        self._max_chars = validate_max_chars(max_input_chars)

    async def decide(self, text: str) -> RouteDecision:
        early = precheck_input(text, self._max_chars)
        if early is not None:
            return early
        matched = [route for route, pattern in RULES if pattern.search(text)]
        if not matched:
            return fallback(RouteReason.no_match)
        if len(matched) > 1:
            # Conflicting evidence is not a decision.
            return fallback(RouteReason.low_confidence, CONFLICT_CONFIDENCE)
        return RouteDecision(matched[0], RULE_CONFIDENCE, RouteReason.rule_match, False)
