"""The research run service end to end with fakes only (no socket, no real model or search)."""

import asyncio
import functools
import logging
from pathlib import Path
from uuid import uuid4

import pytest
from research_run_support import (
    AGREEING_CLAIMS,
    AGREEING_HITS,
    AGREEING_PAGES,
    MARKER_TEXT,
    QUESTION,
    SIXTY_A,
    URL_A,
    FakeSearch,
    Gate,
    Harness,
    ScriptedLLM,
)

from backend.research.models import FailureReason, ResearchLevel, ResearchStatus
from backend.research.quick import QuickLimits
from backend.research.reader import ReaderError, ReadFailure
from backend.research.run_control import BudgetExhausted, Busy, NotCancellable, UnknownSession
from backend.research.runner import (
    NO_VERIFIED_CLAIM_NOTICES,
    QUICK_NO_CLAIM_NOTICE,
    goal_for,
    has_notice_of_no_verified_claims,
    session_id_from_goal,
)
from backend.research.search import SearchFailure
from backend.research.search_budget import BudgetedSearchProvider
from backend.research.standard import StandardLimits
from backend.tasks.models import StepStatus, TaskFailure, TaskStatus, VerificationState
from backend.tasks.queue import ExecutionOutcome

pytestmark = pytest.mark.usefixtures("no_network")


def aio(test):
    """Run an async test on a fresh event loop (the suite has no async plugin)."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


async def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# ----- goal handling -----


def test_the_goal_is_a_fixed_label_and_the_session_id_only() -> None:
    session_id = uuid4()
    assert goal_for(session_id) == f"web research {session_id}"
    assert session_id_from_goal(goal_for(session_id)) == session_id
    for bad in ("", "web research", f"web research {session_id} extra", f"run {session_id}",
                f"web research {str(session_id).upper()}", "web research " + "x" * 36):
        assert session_id_from_goal(bad) is None


def test_notice_detection_needs_a_whole_line() -> None:
    assert has_notice_of_no_verified_claims(f"a\n\n{QUICK_NO_CLAIM_NOTICE}")
    for sentence in NO_VERIFIED_CLAIM_NOTICES:
        assert has_notice_of_no_verified_claims(f"Caveats:\n- {sentence}\n")
    assert not has_notice_of_no_verified_claims(f"almost {QUICK_NO_CLAIM_NOTICE} but not")
    assert not has_notice_of_no_verified_claims("")
    assert not has_notice_of_no_verified_claims(None)


# ----- completed runs -----


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_agreeing_sources_complete_with_verified_citations(
    tmp_path: Path, level: ResearchLevel
) -> None:
    h = Harness(tmp_path)
    session_id = h.service.submit(QUESTION, level)

    pending = h.task_of(session_id)
    assert pending.status is TaskStatus.PENDING
    assert QUESTION not in pending.goal and pending.goal == goal_for(session_id)
    assert [step.description for step in pending.steps][0] == "Plan the search queries"

    task = await h.service.run_one()
    assert task is not None and task.status is TaskStatus.COMPLETED
    assert task.verified is VerificationState.VERIFIED
    assert QUESTION not in (task.result_summary or "")
    assert all(step.status is StepStatus.COMPLETED for step in task.steps[: task.current_step + 1])

    session = h.repository.get_session(session_id)
    assert session.status is ResearchStatus.COMPLETED and session.level is level
    claims = h.repository.list_claims(session_id)
    expected = {text for items in AGREEING_CLAIMS.values() for text, _ in items}
    assert {c.claim_text for c in claims} == expected and SIXTY_A in expected
    sources = {s.id: s for s in h.repository.list_sources(session_id)}
    for claim in claims:
        assert claim.quote and claim.quote_start is not None  # verified against the page text
        assert sources[claim.source_id].final_url in AGREEING_PAGES
    assert h.service.progress(session_id) is None  # nothing live once it is done
    assert h.search.calls  # the injected provider was used, nothing else


@aio
async def test_progress_stages_are_visible_while_running(tmp_path: Path) -> None:
    gate = Gate()
    h = Harness(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate))
    session_id = h.service.submit(QUESTION, ResearchLevel.STANDARD)
    running = asyncio.ensure_future(h.service.run_one())
    await wait_for(lambda: gate.entered)

    progress = h.service.progress(session_id)
    assert progress is not None and progress["stage"] == "searching"
    assert set(progress) == {"stage", "round", "queries", "pages", "sources", "claims"}
    task = h.task_of(session_id)
    assert task.status is TaskStatus.RUNNING and task.current_step == 1

    gate.release()
    done = await running
    assert done.status is TaskStatus.COMPLETED


@aio
async def test_quick_reports_its_stages_through_the_steps(tmp_path: Path) -> None:
    gate = Gate()
    h = Harness(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate))
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    running = asyncio.ensure_future(h.service.run_one())
    await wait_for(lambda: gate.entered)
    assert h.service.progress(session_id)["stage"] == "searching"
    assert h.task_of(session_id).current_step == 1
    gate.release()
    done = await running
    assert done.status is TaskStatus.COMPLETED and done.current_step == 4


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_no_verifiable_claim_but_an_explicit_notice_still_completes(
    tmp_path: Path, level: ResearchLevel
) -> None:
    h = Harness(tmp_path, llm=ScriptedLLM({}, insufficient=True))
    session_id = h.service.submit(QUESTION, level)
    task = await h.service.run_one()
    assert task.status is TaskStatus.COMPLETED
    assert h.repository.list_claims(session_id) == []
    text = h.repository.get_session(session_id).result_text
    assert has_notice_of_no_verified_claims(text)


# ----- failures -----


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_every_fetch_failing_fails_with_a_fixed_reason(
    tmp_path: Path, level: ResearchLevel
) -> None:
    pages = {url: ReaderError(ReadFailure.NETWORK_ERROR) for url in AGREEING_PAGES}
    h = Harness(tmp_path, pages=pages)
    session_id = h.service.submit(QUESTION, level)
    task = await h.service.run_one()
    session = h.repository.get_session(session_id)
    assert session.status is ResearchStatus.FAILED
    assert session.failure_reason is FailureReason.READER_FAILED
    assert session.result_text is None
    assert task.status is TaskStatus.FAILED and task.failure_code is TaskFailure.EXECUTION_FAILED


@aio
async def test_a_search_provider_failure_is_a_fixed_search_failed(tmp_path: Path) -> None:
    h = Harness(tmp_path, search=FakeSearch(failure=SearchFailure.UNAVAILABLE))
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    await h.service.run_one()
    assert h.repository.get_session(session_id).failure_reason is FailureReason.SEARCH_FAILED


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_an_invented_quote_is_dropped_and_the_run_fails_not_completes(
    tmp_path: Path, level: ResearchLevel
) -> None:
    invented = {URL_A: [("The cache keeps entries forever.", "entries are kept forever and ever")]}
    h = Harness(tmp_path, llm=ScriptedLLM(invented))
    session_id = h.service.submit(QUESTION, level)
    task = await h.service.run_one()
    session = h.repository.get_session(session_id)
    assert session.status is ResearchStatus.FAILED
    assert session.failure_reason is FailureReason.SYNTHESIS_FAILED
    assert h.repository.list_claims(session_id) == []
    assert task.status is TaskStatus.FAILED


@aio
async def test_a_model_error_is_a_fixed_synthesis_failure_without_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(tmp_path, llm=ScriptedLLM(error=RuntimeError(f"upstream said {MARKER_TEXT}")))
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    await h.service.run_one()
    session = h.repository.get_session(session_id)
    assert session.failure_reason is FailureReason.SYNTHESIS_FAILED
    assert MARKER_TEXT not in caplog.text


@aio
async def test_a_foreign_task_and_a_deep_session_are_failed_not_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    foreign = h.queue.submit("something else entirely", ["one step"])
    deep = h.repository.create_session(QUESTION, ResearchLevel.DEEP)
    h.queue.submit(goal_for(deep.id), ["one step"])
    first = await h.service.run_one()
    second = await h.service.run_one()
    assert first.id == foreign.id and first.status is TaskStatus.FAILED
    assert second.status is TaskStatus.FAILED
    assert h.search.calls == []  # neither reached the search provider
    assert h.repository.get_session(deep.id).status is ResearchStatus.FAILED  # settled


@aio
async def test_the_verifier_refuses_completion_without_claims_or_notice(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    verifier = h.service._verifier
    session = h.repository.create_session(QUESTION, ResearchLevel.QUICK)
    task = h.queue.submit(goal_for(session.id), ["one step"])
    outcome = ExecutionOutcome.succeeded("claimed")

    assert not (await verifier.verify(task, outcome)).passed  # still pending
    h.repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    h.repository.set_result(session.id, "A confident answer with nothing behind it.")
    assert not (await verifier.verify(task, outcome)).passed  # completed, no claims, no notice

    other = h.repository.create_session(QUESTION, ResearchLevel.QUICK)
    other_task = h.queue.submit(goal_for(other.id), ["one step"])
    h.repository.transition(other.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    h.repository.set_result(other.id, f"Sorry.\n\n{QUICK_NO_CLAIM_NOTICE}")
    assert (await verifier.verify(other_task, outcome)).passed  # the explicit notice

    foreign = h.queue.submit("not ours", ["one step"])
    assert not (await verifier.verify(foreign, outcome)).passed


# ----- budget and busy -----


@aio
async def test_an_exhausted_budget_is_refused_before_anything_is_queued(tmp_path: Path) -> None:
    inner = FakeSearch(AGREEING_HITS)
    guarded = BudgetedSearchProvider(inner, monthly_limit=5, usage_counter=lambda: 5)
    h = Harness(tmp_path, search=guarded, is_budget_exhausted=guarded.is_exhausted)
    with pytest.raises(BudgetExhausted):
        h.service.submit(QUESTION, ResearchLevel.QUICK)
    assert h.repository.list_sessions() == []
    assert h.tasks.list_tasks() == []
    assert inner.calls == []


@aio
async def test_budget_running_out_mid_run_fails_without_reaching_the_vendor(
    tmp_path: Path,
) -> None:
    used = {"n": 0}
    inner = FakeSearch(AGREEING_HITS)
    guarded = BudgetedSearchProvider(inner, monthly_limit=5, usage_counter=lambda: used["n"])
    h = Harness(tmp_path, search=guarded, is_budget_exhausted=guarded.is_exhausted)
    session_id = h.service.submit(QUESTION, ResearchLevel.STANDARD)
    used["n"] = 5  # another client spent the rest after the request was accepted
    await h.service.run_one()
    assert h.repository.get_session(session_id).failure_reason is FailureReason.SEARCH_FAILED
    assert inner.calls == []


@aio
async def test_only_one_research_at_a_time(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.service.submit(QUESTION, ResearchLevel.QUICK)
    with pytest.raises(Busy):
        h.service.submit("another question", ResearchLevel.QUICK)
    assert len(h.repository.list_sessions()) == 1

    await h.service.run_one()
    assert h.repository.get_session(first).status is ResearchStatus.COMPLETED
    second = h.service.submit("another question", ResearchLevel.QUICK)  # free again
    assert second != first


def test_unavailable_levels_are_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for level in (ResearchLevel.DEEP, ResearchLevel.EXTENSIVE, ResearchLevel.MEMORY):
        with pytest.raises(ValueError):
            h.service.submit(QUESTION, level)
    assert h.repository.list_sessions() == []


# ----- cancellation -----


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_cancel_while_running(tmp_path: Path, level: ResearchLevel) -> None:
    gate = Gate()
    h = Harness(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate))
    session_id = h.service.submit(QUESTION, level)
    running = asyncio.ensure_future(h.service.run_one())
    await wait_for(lambda: gate.entered)

    assert h.service.cancel(session_id) == "cancelling"
    task = await running
    assert task.status is TaskStatus.CANCELLED
    assert h.repository.get_session(session_id).status is ResearchStatus.CANCELLED
    assert h.repository.list_claims(session_id) == []
    assert h.service.progress(session_id) is None
    with pytest.raises(NotCancellable):
        h.service.cancel(session_id)  # already final


@aio
async def test_cancel_before_it_starts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    assert h.service.cancel(session_id) == "cancelled"
    assert h.task_of(session_id).status is TaskStatus.CANCELLED
    assert h.repository.get_session(session_id).status is ResearchStatus.CANCELLED
    assert await h.service.run_one() is None  # nothing left to claim
    assert h.search.calls == []
    h.service.submit(QUESTION, ResearchLevel.QUICK)  # and the slot is free again


def test_cancel_of_an_unknown_session(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(UnknownSession):
        h.service.cancel(uuid4())


# ----- restart and shutdown -----


@aio
async def test_recovery_fails_interrupted_work_and_never_reruns_it(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    crashed = h.service.submit(QUESTION, ResearchLevel.QUICK)
    claimed = h.tasks.claim_next()  # a process died after claiming it
    h.repository.transition(crashed, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    assert claimed is not None

    waiting = h.repository.create_session(QUESTION, ResearchLevel.QUICK)  # orphan: no task
    queued = h.repository.create_session(QUESTION, ResearchLevel.QUICK)
    queued_task = h.queue.submit(goal_for(queued.id), ["s"])

    assert h.service.recover() == 1
    assert h.task_of(crashed).failure_code is TaskFailure.INTERRUPTED
    assert h.repository.get_session(crashed).status is ResearchStatus.FAILED
    assert h.repository.get_session(crashed).failure_reason is FailureReason.INTERNAL_ERROR
    assert h.repository.get_session(waiting.id).status is ResearchStatus.FAILED
    assert h.repository.get_session(queued.id).status is ResearchStatus.PENDING  # still queued
    assert h.tasks.get_task(queued_task.id).status is TaskStatus.PENDING
    assert h.search.calls == []  # nothing was re-run by recovering


@aio
async def test_the_worker_runs_a_request_and_stops_cleanly_when_asked(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(h.service.run_worker(stop))
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    await wait_for(lambda: h.repository.get_session(session_id).status.value == "completed")
    stop.set()
    await asyncio.wait_for(worker, timeout=2)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


@aio
async def test_cancelling_the_worker_mid_run_leaves_no_orphans(tmp_path: Path) -> None:
    gate = Gate()
    h = Harness(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate))
    worker = asyncio.ensure_future(h.service.run_worker())
    session_id = h.service.submit(QUESTION, ResearchLevel.STANDARD)
    await wait_for(lambda: gate.entered)

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert h.task_of(session_id).status is TaskStatus.FAILED
    assert h.task_of(session_id).failure_code is TaskFailure.INTERRUPTED
    assert h.repository.get_session(session_id).status in (
        ResearchStatus.CANCELLED,
        ResearchStatus.FAILED,
    )
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert h.queue._tokens == {}


@pytest.mark.parametrize("level", [ResearchLevel.QUICK, ResearchLevel.STANDARD])
@aio
async def test_the_research_time_budget_ends_the_run_as_timeout(
    tmp_path: Path, level: ResearchLevel
) -> None:
    gate = Gate()
    h = Harness(
        tmp_path,
        search=FakeSearch(AGREEING_HITS, gate=gate),
        quick_limits=QuickLimits(total_timeout=0.2),
        standard_limits=StandardLimits(total_timeout=0.2),
    )
    session_id = h.service.submit(QUESTION, level)
    task = await h.service.run_one()
    session = h.repository.get_session(session_id)
    assert session.status is ResearchStatus.FAILED
    assert session.failure_reason is FailureReason.TIMEOUT
    assert task.status is TaskStatus.FAILED


@aio
async def test_the_queue_backstop_timeout_never_leaves_a_session_active(tmp_path: Path) -> None:
    gate = Gate()
    h = Harness(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate), queue_timeout=0.3)
    session_id = h.service.submit(QUESTION, ResearchLevel.QUICK)
    task = await h.service.run_one()
    assert task.status is TaskStatus.FAILED and task.failure_code is TaskFailure.TIMEOUT
    assert h.repository.get_session(session_id).status in (
        ResearchStatus.CANCELLED,
        ResearchStatus.FAILED,
    )
    h.service.submit(QUESTION, ResearchLevel.QUICK)  # not busy any more


# ----- no leakage -----


@aio
async def test_logs_never_contain_the_question_urls_or_page_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    question = f"{QUESTION} {MARKER_TEXT}"
    pages = {url: ReaderError(ReadFailure.NETWORK_ERROR) for url in AGREEING_PAGES}
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    for harness in (Harness(tmp_path / "ok"), Harness(tmp_path / "bad", pages=pages)):
        for level in (ResearchLevel.QUICK, ResearchLevel.STANDARD):
            harness.service.submit(question, level)
            await harness.service.run_one()
            harness.service.recover()
    assert MARKER_TEXT not in caplog.text
    assert "docs.a.test" not in caplog.text and "keeps entries" not in caplog.text


def test_the_module_does_not_log_free_text() -> None:
    source = (Path(__file__).parents[1] / "backend" / "research" / "runner.py").read_text()
    assert "question" not in " ".join(
        line for line in source.splitlines() if "logger." in line
    )
