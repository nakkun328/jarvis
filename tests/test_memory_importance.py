"""Importance scale (0-5 -> stored 0-1) and the explainable rule set."""

import pytest

from backend.memory.importance import (
    LEVEL_ANCHORS,
    assess_importance,
    level_to_score,
    score_to_level,
)
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord


def test_every_level_round_trips_and_stays_valid_for_a_record() -> None:
    assert set(LEVEL_ANCHORS) == set(range(6))
    for level in range(6):
        score = level_to_score(level)
        assert score_to_level(score) == level
        MemoryRecord(MemoryCategory.USER, "x", "s", MemoryOrigin.CHAT, score, 0.5)


@pytest.mark.parametrize("bad", [-1, 6, True, 2.5])
def test_levels_outside_the_scale_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        level_to_score(bad)


@pytest.mark.parametrize("bad", [-0.1, 1.1, True, "0.5"])
def test_scores_outside_the_range_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        score_to_level(bad)


def test_an_explicit_remember_request_about_the_user_is_critical() -> None:
    result = assess_importance(
        MemoryCategory.USER, MemoryOrigin.USER_EXPLICIT, "いつも敬語で話すことを覚えて"
    )
    assert result.level == 5 and result.score == 1.0
    assert "asked-to-remember+1" in result.reasons and "standing-rule+1" in result.reasons


def test_a_plain_statement_by_the_user_is_high_but_not_critical() -> None:
    result = assess_importance(
        MemoryCategory.USER, MemoryOrigin.USER_EXPLICIT, "私は京都に住んでいる"
    )
    assert result.level == 4


def test_hedged_statements_are_rated_lower() -> None:
    plain = assess_importance(MemoryCategory.USER, MemoryOrigin.CHAT, "I live in Kyoto")
    hedged = assess_importance(MemoryCategory.USER, MemoryOrigin.CHAT, "I think I live in Kyoto")
    assert hedged.level == plain.level - 1


def test_ai_opinions_and_discussions_are_capped_low() -> None:
    inferred = assess_importance(
        MemoryCategory.USER, MemoryOrigin.AI_INFERENCE, "always prefers short answers"
    )
    assert inferred.level == 2 and any(r.startswith("cap:") for r in inferred.reasons)
    discussion = assess_importance(
        MemoryCategory.CONVERSATION, MemoryOrigin.DISCUSSION, "議論の結果"
    )
    assert discussion.level == 0
    research = assess_importance(
        MemoryCategory.PROJECT, MemoryOrigin.RESEARCH, "今後 always 覚えて always"
    )
    assert research.level <= 3


def test_the_result_is_always_inside_the_scale_and_deterministic() -> None:
    for category in MemoryCategory:
        for origin in MemoryOrigin:
            for text in ("", "x", "覚えて いつも", "たぶん maybe"):
                first = assess_importance(category, origin, text)
                assert first == assess_importance(category, origin, text)
                assert 0 <= first.level <= 5 and 0 <= first.score <= 1
