from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class ExecutionRequest:
    model: str
    workspace: str
    title: str
    instructions: str
    permission_profile: str
    allow_write: bool
    timeout_seconds: float


@dataclass
class ExecutionResult:
    session_id: str | None
    final_text: str
    termination_reason: str | None
    exit_code: int | None
    timed_out: bool
    outcome: str
    events_path: str | None
    event_count: int
    error: str | None
    started_at: str
    finished_at: str
    process_group_id: int | None = None
    orphans_detected: bool = False


class AgentAdapter(Protocol):
    name: str

    def run(
        self,
        request: ExecutionRequest,
        *,
        events_path: str,
        on_session: Callable[[str], None],
    ) -> ExecutionResult: ...

    def recover(self, events_path: str) -> ExecutionResult | None: ...
