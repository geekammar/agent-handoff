from __future__ import annotations

import sys
import time
from typing import Any, Iterable, TextIO

from ..util import sanitize_error, utcnow_iso


class ProgressReporter:
    """Human-readable dispatch progress written outside the JSON stdout contract."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        stream: TextIO | None = None,
        clock: Any = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.stream = stream or sys.stderr
        self.clock = clock or time.monotonic
        self.started_at = self.clock()
        self.started_iso = utcnow_iso()
        self._redactions: list[str] = []

    def add_redaction(self, value: str | None) -> None:
        value = (value or "").strip()
        if value and value not in self._redactions:
            self._redactions.append(value)
        self._redactions.sort(key=len, reverse=True)

    def add_redactions(self, values: Iterable[str | None]) -> None:
        for value in values:
            self.add_redaction(value)

    def reset(self) -> None:
        self.started_at = self.clock()
        self.started_iso = utcnow_iso()

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    def _redact(self, text: str) -> str:
        for value in self._redactions:
            text = text.replace(value, "<local>")
        return text

    def redact(self, text: str) -> str:
        return self._redact(str(text or ""))

    def emit(self, stage: str, message: str, *, issue: int | None = None) -> None:
        if not self.enabled:
            return
        stage = sanitize_error(stage or "status", 40)
        message = sanitize_error(message or "", 1000)
        if not message:
            return
        location = f"issue #{int(issue)} " if issue is not None else ""
        line = f"[{self.elapsed():8.1f}s] {location}{stage}: {message}"
        try:
            print(self._redact(line), file=self.stream, flush=True)
        except OSError:
            pass


NULL_PROGRESS = ProgressReporter(enabled=False)
