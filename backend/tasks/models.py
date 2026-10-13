"""Task records and their state machine.

These are plain, immutable values. A task goal is untrusted data: it is stored
and handed to an executor verbatim and is never interpreted by this package.
Failure and waiting causes are fixed codes; free-form error text is never kept.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

MAX_GOAL_CHARS = 2000
MAX_STEP_DESCRIPTION_CHARS = 500
MAX_STEP_NOTE_CHARS = 500
MAX_RESULT_SUMMARY_CHARS = 4000
MAX_TARGET_DEVICE_CHARS = 100
MAX_STEPS = 50
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 10


#: Tasks created from chat start with this label (then the conversation id and a job id). They are
#: run only by the chat-tool worker: `claim_next` skips them unless that prefix is asked for.
CHAT_TOOL_GOAL_LABEL = "chat tool"
CHAT_TOOL_GOAL_PREFIX = CHAT_TOOL_GOAL_LABEL + " "
_CHAT_TOOL_GOAL = re.compile(
    r"chat tool ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) ([0-9a-f]{32})"
)


def chat_tool_goal(conversation_id: UUID, job_id: str) -> str:
    """Fixed label, the conversation id and a job id. Never any user text."""
    return f"{CHAT_TOOL_GOAL_PREFIX}{conversation_id} {job_id}"


def parse_chat_tool_goal(goal: str) -> tuple[UUID, str] | None:
    """(conversation id, job id) of a chat-tool task, or None for any other task."""
    match = _CHAT_TOOL_GOAL.fullmatch(goal)
    if match is None:
        return None
    return UUID(match.group(1)), match.group(2)


#: Group discussions (docs/discussion.md) are tasks too: label, conversation id, job id, no user
#: text. Only the discussion worker claims them, and the chat-tool worker never does.
DISCUSSION_GOAL_LABEL = "group discussion"
DISCUSSION_GOAL_PREFIX = DISCUSSION_GOAL_LABEL + " "
_DISCUSSION_GOAL = re.compile(
    r"group discussion ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r" ([0-9a-f]{32})"
)
#: Goal prefixes that the plain `claim_next()` (no prefix) must never hand out.
RESERVED_GOAL_PREFIXES = (CHAT_TOOL_GOAL_PREFIX, DISCUSSION_GOAL_PREFIX)


def discussion_goal(conversation_id: UUID, job_id: str) -> str:
    """Fixed label, the conversation id and a job id. Never any user text."""
    return f"{DISCUSSION_GOAL_PREFIX}{conversation_id} {job_id}"


def parse_discussion_goal(goal: str) -> tuple[UUID, str] | None:
    """(conversation id, job id) of a discussion task, or None for any other task."""
    match = _DISCUSSION_GOAL.fullmatch(goal)
    if match is None:
        return None
    return UUID(match.group(1)), match.group(2)


def conversation_of_goal(goal: str) -> UUID | None:
    """The conversation a chat-tool task belongs to, or None for any other task."""
    parsed = parse_chat_tool_goal(goal)
    return parsed[0] if parsed else None


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskFailure(StrEnum):
    """Why a task failed. Upstream error messages are never stored."""

    EXECUTION_FAILED = "execution_failed"
    VERIFICATION_FAILED = "verification_failed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class WaitingReason(StrEnum):
    NEEDS_CONFIRMATION = "needs_confirmation"
    NEEDS_INPUT = "needs_input"
    DEPENDENCY = "dependency"


class VerificationState(StrEnum):
    """Outcome of the postcondition check, separate from the executor's own claim."""

    NOT_VERIFIED = "not_verified"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.COMPLETED, TaskStatus.CANCELLED})

#: Step statuses an executor may finish a running step with.
FINISHING_STEP_STATUSES = frozenset({StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED})

ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class TaskStep:
    index: int
    description: str
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str | None = None


@dataclass(frozen=True)
class Task:
    id: UUID
    goal: str
    steps: tuple[TaskStep, ...]
    current_step: int | None
    target_device: str | None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_summary: str | None = None
    failure_code: TaskFailure | None = None
    waiting_reason: WaitingReason | None = None
    attempt: int = 1
    verified: VerificationState = VerificationState.NOT_VERIFIED
    retry_of: UUID | None = None
