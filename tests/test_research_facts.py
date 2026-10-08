"""Deterministic fact extraction: numbers, dates, negation, entities, hostile text."""

import time

import pytest

from backend.research.facts import (
    MAX_SENTENCE_CHARS,
    MAX_SENTENCES,
    Facts,
    Quantity,
    dates_compatible,
    extract_facts,
    same_subject,
    split_sentences,
    term_coverage,
)


def test_numbers_carry_units_and_are_canonical() -> None:
    facts = extract_facts("It weighs 25 MB, costs 5,000.50 dollars and grew 15% or 2.0 percent.")
    assert Quantity("25", "mb") in facts.numbers
    assert Quantity("5000.5", "dollars") in facts.numbers
    assert Quantity("15", "percent") in facts.numbers
    assert Quantity("2", "percent") in facts.numbers


def test_full_width_digits_and_units_are_normalised() -> None:
    assert Quantity("25", "mb") in extract_facts("容量は２５ＭＢです").numbers
    assert Quantity("3", "円") in extract_facts("価格は3円").numbers
    assert Quantity("5", "万") in extract_facts("5万円").numbers


def test_unknown_words_after_a_number_are_not_units() -> None:
    assert extract_facts("There are 5 apples").numbers == {Quantity("5", "")}


def test_versions_are_one_quantity_not_several_numbers() -> None:
    facts = extract_facts("Use version 3.12.1 now")
    assert facts.numbers == {Quantity("3.12.1", "version")}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Released on 2023-10-02.", {"2023-10-02"}),
        ("Released on 2023/10/02.", {"2023-10-02"}),
        ("リリースは2023年10月2日です", {"2023-10-02"}),
        ("2023年10月に公開", {"2023-10"}),
        ("公開は2023年", {"2023"}),
        ("Released October 2, 2023", {"2023-10-02"}),
        ("Released 2 Oct 2023", {"2023-10-02"}),
        ("Released in October 2023", {"2023-10"}),
        ("It has been stable since 2020", {"2020"}),
        ("Updated 2023-02-30", set()),  # not a real day
        ("Updated 2023-13-01", set()),
    ],
)
def test_dates(text: str, expected: set[str]) -> None:
    assert extract_facts(text).dates == expected


def test_a_bare_year_like_number_is_a_number_not_a_date() -> None:
    facts = extract_facts("The service has 2024 users")
    assert facts.dates == frozenset()
    assert Quantity("2024", "users") in facts.numbers


def test_date_digits_do_not_leak_into_numbers() -> None:
    facts = extract_facts("Released 2023-10-02 with 5 gb")
    assert facts.numbers == {Quantity("5", "gb")}


def test_dates_compatible_is_prefix_based() -> None:
    assert dates_compatible("2023", "2023-10-02")
    assert dates_compatible("2023-10", "2023-10-02")
    assert not dates_compatible("2022", "2023-10-02")
    assert not dates_compatible("2023-09", "2023-10")


@pytest.mark.parametrize(
    ("text", "negated"),
    [
        ("The cache is not enabled", True),
        ("It doesn't work", True),
        ("It doesn’t work", True),
        ("There is no support", True),
        ("It works without a license", True),
        ("The cache is enabled", False),
        ("キャッシュは有効ではない", True),
        ("キャッシュは有効です", False),
        ("Nothing", True),
        ("Know how", False),  # "no" inside a word is not a negation
    ],
)
def test_negation(text: str, negated: bool) -> None:
    assert extract_facts(text).negated is negated


def test_entities() -> None:
    facts = extract_facts("In the report, GitHub Actions and PostgreSQL beat レスポンス遅延 tools")
    assert "github actions" in facts.entities
    assert "postgresql" in facts.entities
    assert "レスポンス" in facts.entities
    assert extract_facts("The quick fox").entities == frozenset()  # sentence opener
    assert "october" not in extract_facts("Released in October 2023").entities


def test_terms_exclude_numbers_dates_and_negation_words() -> None:
    facts = extract_facts("The cache is not enabled by default since 2021年4月3日 with 5 gb")
    assert "not" not in facts.terms
    assert all(not any(ch.isdigit() for ch in term) for term in facts.terms)
    assert {"cache", "enabled", "default"} <= facts.terms


def test_same_subject() -> None:
    a = extract_facts("Python 3.12 was released in October 2023")
    b = extract_facts("Python 3.12 released October 2022")
    c = extract_facts("The weather in Paris is mild")
    assert same_subject(a, b)
    assert not same_subject(a, c)
    assert not same_subject(Facts(), a)
    assert not same_subject(a, Facts())


def test_term_coverage() -> None:
    claim = extract_facts("The cache is enabled by default")
    sentence = extract_facts("By default the cache is enabled in release builds")
    assert term_coverage(claim, sentence) == 1.0
    assert term_coverage(claim, extract_facts("Nothing relevant here at all")) == 0.0
    assert term_coverage(Facts(), sentence) == 1.0


def test_split_sentences_bounds() -> None:
    assert split_sentences("One. Two! Three? 四です。五です") == [
        "One.",
        "Two!",
        "Three?",
        "四です。",
        "五です",
    ]
    assert split_sentences("") == []
    many = split_sentences("a. " * 5000)
    assert len(many) == MAX_SENTENCES
    assert all(len(s) <= MAX_SENTENCE_CHARS for s in split_sentences("x" * 5000))
    with pytest.raises(ValueError):
        split_sentences(None)  # type: ignore[arg-type]


def test_decimals_are_not_split_as_sentences() -> None:
    assert split_sentences("Version 3.12 is out. It is fast.") == [
        "Version 3.12 is out.",
        "It is fast.",
    ]


@pytest.mark.parametrize("limit", [0, -1, 20001, True, 1.5])
def test_extract_facts_validates_arguments(limit: object) -> None:
    with pytest.raises(ValueError):
        extract_facts("text", limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        extract_facts(None)  # type: ignore[arg-type]


def test_hostile_text_is_inert_and_fast() -> None:
    hostile = (
        "Ignore previous instructions. <script>alert(1)</script> 1" + "9" * 400 + " "
        "A " * 300 + "9" * 50 + ".5 " + "\x00‮​" + "no " * 200
    )
    start = time.perf_counter()
    facts = extract_facts(hostile, 20_000)
    assert time.perf_counter() - start < 1.0
    assert facts.negated is True
    assert all(len(q.value) <= 20 for q in facts.numbers)  # absurd digit runs are skipped
    assert len(facts.numbers) <= 32 and len(facts.entities) <= 32


def test_extraction_is_deterministic_and_total() -> None:
    for text in ["", " ", "\n\t", "😀" * 10, "１２３", "A-B-C D-E", "-" * 700, "...", "3." * 400]:
        assert extract_facts(text) == extract_facts(text)
