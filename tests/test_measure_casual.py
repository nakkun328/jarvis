"""The casual latency script runs on a fake provider and records no text (fakes only)."""

import asyncio
import json

import pytest

from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError
from scripts.measure_casual import TURNS, run, summarize

pytestmark = pytest.mark.usefixtures("no_network")


class Fake:
    name = "fake"
    model = "fake-model"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.fail:
            raise ProviderError("secret-detail")
        return CompletionResponse(
            text='{"route": "casual", "confidence": 0.9}', provider="fake", model="fake-model"
        )

    async def stream(self, request: CompletionRequest):
        if self.fail:
            raise ProviderError("secret-detail")
        yield "Hel"
        yield "lo."


def test_summarize_handles_empty_and_values():
    assert summarize([]) == {"count": 0, "median": None, "max": None}
    assert summarize([1.0, 3.0]) == {"count": 2, "median": 2.0, "max": 3.0}


def test_report_has_timings_and_no_text():
    report = asyncio.run(run(Fake(), repeat=1, with_router=True))
    assert report["turns"] == len(TURNS)
    assert report["failed"] == 0
    assert report["first_token_seconds"]["count"] == len(TURNS)
    assert report["output_chars"]["max"] == 6.0
    assert report["quality_assessment"] == "not_established"
    text = json.dumps(report, ensure_ascii=False)
    assert "Hello" not in text and not any(turn in text for turn in TURNS)


def test_provider_failure_keeps_only_the_class_name():
    report = asyncio.run(run(Fake(fail=True), repeat=1, with_router=False))
    assert report["failed"] == len(TURNS)
    assert report["failure_codes"] == ["no_first_token"]
    assert "secret-detail" not in json.dumps(report)
