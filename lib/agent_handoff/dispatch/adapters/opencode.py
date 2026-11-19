from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ...util import ensure_dir, sanitize_error, utc_from_timestamp, utcnow_iso
from .base import ExecutionRequest, ExecutionResult

READ_ONLY_POLICY = {
    "share": "disabled",
    "permission": {
        "*": "deny",
        "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "**/.env*": "deny"},
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
    },
}

WORKSPACE_WRITE_POLICY = {
    "share": "disabled",
    "permission": {
        "*": "deny",
        "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "**/.env*": "deny"},
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "edit": "allow",
    },
}

BAD_TERMINATION_REASONS = {"error", "length", "max_tokens", "content_filter", "timeout", "cancelled"}


class OpenCodeEventParser:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.termination_reason: str | None = None
        self.event_count = 0
        self.errors: list[str] = []
        self._messages: dict[str, list[str]] = {}
        self._order: list[str] = []
        self._last_message_id: str | None = None

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        self.event_count += 1
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        session_id = event.get("sessionID") or part.get("sessionID")
        if isinstance(session_id, str) and session_id and not self.session_id:
            self.session_id = session_id
        event_type = event.get("type")
        message_id = part.get("messageID") or event.get("messageID")
        if isinstance(message_id, str) and message_id:
            self._last_message_id = message_id
        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                key = message_id if isinstance(message_id, str) and message_id else "_"
                if key not in self._messages:
                    self._order.append(key)
                    self._messages[key] = []
                self._messages[key].append(text)
        elif event_type == "step_finish":
            reason = part.get("reason")
            if isinstance(reason, str) and reason:
                self.termination_reason = reason
        elif event_type == "error":
            error = event.get("error") or part.get("error") or event.get("message")
            if isinstance(error, dict):
                error = error.get("message") or error.get("name")
            if error:
                self.errors.append(str(error))

    def final_text(self) -> str:
        for key in reversed(self._order):
            text = "\n".join(part for part in self._messages.get(key, []) if part)
            if text.strip():
                return text.strip()
        return ""

    def error_text(self) -> str | None:
        if not self.errors:
            return None
        return "; ".join(sanitize_error(item, 600) for item in self.errors[:5])


def _policy(profile: str, allow_write: bool) -> dict[str, Any]:
    if profile == "workspace_write":
        if not allow_write:
            raise ValueError("workspace_write requires allow_write=true")
        return WORKSPACE_WRITE_POLICY
    if allow_write:
        raise ValueError("allow_write=true requires permission_profile=workspace_write")
    return READ_ONLY_POLICY


def _outcome(parser: OpenCodeEventParser, exit_code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "blocked"
    if exit_code not in (0, None):
        return "failed"
    reason = (parser.termination_reason or "").lower()
    if reason in BAD_TERMINATION_REASONS:
        return "blocked"
    if parser.final_text():
        return "succeeded"
    return "blocked"


class OpenCodeAdapter:
    name = "opencode"

    def __init__(self, execution: dict[str, Any]):
        self.execution = execution
        self.permission_profile = execution.get("permission_profile", "read_only")
        self.allow_write = bool(execution.get("allow_write", False))
        self.timeout_seconds = float(execution.get("timeout_seconds", 1200))

    def _command(self, request: ExecutionRequest) -> list[str]:
        binary = os.environ.get("AGENT_HANDOFF_OPENCODE_BIN") or "opencode"
        agent = "build" if self.permission_profile == "workspace_write" else "plan"
        prompt = "Task submitted through Agent Handoff:\n\n" + request.instructions
        return [
            binary,
            "run",
            "--pure",
            "--agent",
            agent,
            "--model",
            request.model,
            "--format",
            "json",
            "--title",
            request.title,
            prompt,
        ]

    def _environment(self, request: ExecutionRequest) -> dict[str, str]:
        env = os.environ.copy()
        env["PWD"] = request.workspace
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(_policy(self.permission_profile, self.allow_write))
        return env

    def run(
        self,
        request: ExecutionRequest,
        *,
        events_path: str,
        on_session: Callable[[str], None],
    ) -> ExecutionResult:
        workspace = os.path.realpath(request.workspace)
        if not os.path.isdir(workspace):
            raise ValueError("execution workspace does not exist")
        if workspace != request.workspace:
            raise ValueError("execution workspace must be a canonical path")
        ensure_dir(Path(events_path).parent)
        parser = OpenCodeEventParser()
        started_at = utcnow_iso()
        descriptor = os.open(events_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        handle = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        session_lock = threading.Lock()
        session_seen = {"value": None}
        callback_errors: list[str] = []

        def read_stdout(stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    handle.write(line)
                    handle.flush()
                    parser.feed_line(line)
                    if parser.session_id:
                        with session_lock:
                            if session_seen["value"] != parser.session_id:
                                session_seen["value"] = parser.session_id
                                try:
                                    on_session(parser.session_id)
                                except Exception as exc:
                                    callback_errors.append(sanitize_error(str(exc), 600))
            finally:
                stream.close()

        def read_stderr(stream: Any) -> None:
            try:
                for _ in iter(stream.readline, ""):
                    if len(stderr_lines) < 200:
                        stderr_lines.append(_)
            finally:
                stream.close()

        stderr_lines: list[str] = []
        timed_out = False
        exit_code: int | None = None
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                self._command(request),
                cwd=workspace,
                env=self._environment(request),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            stdout_thread = threading.Thread(target=read_stdout, args=(process.stdout,), daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            try:
                exit_code = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    process.terminate()
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    exit_code = process.wait(timeout=10)
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
        finally:
            handle.close()
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
        finished_at = utcnow_iso()
        error_parts = []
        if parser.error_text():
            error_parts.append(parser.error_text() or "")
        if timed_out:
            error_parts.append(f"execution timed out after {request.timeout_seconds:g}s")
        if exit_code not in (0, None):
            tail = sanitize_error("".join(stderr_lines[-20:]), 800)
            error_parts.append(f"opencode exited {exit_code}" + (f": {tail}" if tail else ""))
        if callback_errors:
            error_parts.append("session state update failed: " + "; ".join(callback_errors[:2]))
        error = sanitize_error("\n".join(part for part in error_parts if part.strip())) or None
        return ExecutionResult(
            session_id=parser.session_id,
            final_text=parser.final_text(),
            termination_reason=parser.termination_reason,
            exit_code=exit_code,
            timed_out=timed_out,
            outcome=_outcome(parser, exit_code, timed_out),
            events_path=events_path,
            event_count=parser.event_count,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
        )

    def recover(self, events_path: str) -> ExecutionResult | None:
        path = Path(events_path)
        if not path.is_file():
            return None
        parser = OpenCodeEventParser()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parser.feed_line(line)
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if not parser.session_id or not parser.final_text():
            return None
        finished_at = utc_from_timestamp(mtime)
        outcome = _outcome(parser, None, False) if parser.termination_reason else "blocked"
        return ExecutionResult(
            session_id=parser.session_id,
            final_text=parser.final_text(),
            termination_reason=parser.termination_reason,
            exit_code=None,
            timed_out=False,
            outcome=outcome,
            events_path=events_path,
            event_count=parser.event_count,
            error=parser.error_text(),
            started_at=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            finished_at=finished_at,
        )
