from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from ..util import sha256_text
from .config import resolve_dispatch_project, resolve_model

REQUEST_START = "<!-- agent-handoff-request -->"
REQUEST_END = "<!-- /agent-handoff-request -->"
KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
ALLOWED_KEYS = {
    "handoff-version",
    "project",
    "agent",
    "model",
    "session",
    "task",
}
SUPPORTED_TASKS = {"read_only", "coding"}
REQUIRED_KEYS = {"handoff-version", "project", "agent", "session"}
SUPPORTED_VERSION = "1"


class RequestError(ValueError):
    pass


@dataclass(frozen=True)
class IssueRequest:
    version: str
    project: str
    agent: str
    model_alias: str | None
    session: str
    task: str
    instructions: str
    body_sha256: str


@dataclass(frozen=True)
class ResolvedRequest:
    request: IssueRequest
    project: dict[str, Any]
    model: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def detect_request(body: str) -> bool:
    return REQUEST_START in (body or "")


def parse_issue_request(body: str) -> IssueRequest | None:
    """Parse one versioned request metadata block.

    A body without the explicit marker is not a handoff request and is ignored.
    Once the marker is present, malformed content is rejected rather than
    guessed from a title, labels, or free-form prose.
    """

    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if REQUEST_START not in text:
        return None
    start = text.index(REQUEST_START) + len(REQUEST_START)
    end = text.find(REQUEST_END, start)
    if end == -1:
        raise RequestError("request metadata block is missing its closing marker")
    metadata_text = text[start:end]
    instructions = text[end + len(REQUEST_END) :].strip()
    if not instructions:
        raise RequestError("task instructions are empty")

    metadata: dict[str, str] = {}
    for raw_line in metadata_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = KEY_RE.match(line)
        if not match:
            raise RequestError(f"invalid request metadata line: {line[:80]!r}")
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key not in ALLOWED_KEYS:
            raise RequestError(f"unknown request metadata field: {key!r}")
        if key in metadata:
            raise RequestError(f"duplicate request metadata field: {key!r}")
        if not value:
            raise RequestError(f"request metadata field {key!r} is empty")
        metadata[key] = value

    missing = sorted(REQUIRED_KEYS - set(metadata))
    if missing:
        raise RequestError("missing request metadata: " + ", ".join(missing))
    if metadata["handoff-version"] != SUPPORTED_VERSION:
        raise RequestError(f"unsupported handoff-version {metadata['handoff-version']!r}; expected '1'")
    session = metadata["session"].strip().lower()
    if session == "same":
        raise RequestError(
            "SAME SESSION is not supported by Agent Handoff v1.1; submit session: new. "
            "Safe continuation is deferred rather than simulated."
        )
    if session != "new":
        raise RequestError("session must be 'new'; other session modes are not supported")
    task = (metadata.get("task") or "read_only").strip().lower()
    if task not in SUPPORTED_TASKS:
        raise RequestError(
            f"unknown task type {task!r}; supported values are read_only and coding"
        )

    return IssueRequest(
        version=metadata["handoff-version"],
        project=metadata["project"],
        agent=metadata["agent"].strip().lower(),
        model_alias=metadata.get("model"),
        session=session,
        task=task,
        instructions=instructions,
        body_sha256=_sha256(text),
    )


def resolve_request(request: IssueRequest, dispatch: dict[str, Any]) -> ResolvedRequest:
    if request.agent != "opencode":
        raise RequestError(f"unsupported agent {request.agent!r}; only 'opencode' is supported")
    if request.agent != dispatch.get("agent"):
        raise RequestError("requested agent does not match the configured dispatcher agent")
    project = resolve_dispatch_project(dispatch, request.project)
    if not project:
        raise RequestError(f"unknown project {request.project!r}; the dispatcher only routes explicitly configured projects")
    if request.task == "coding":
        coding = project.get("coding") or {}
        if not coding.get("enabled"):
            raise RequestError(
                "coding tasks are disabled for this project; an owner must explicitly enable "
                "dispatch.coding and the project coding policy"
            )
        selected_alias = (request.model_alias or dispatch.get("default_model") or "").strip()
        allowed = list(coding.get("allowed_model_aliases") or [])
        if not selected_alias or selected_alias not in allowed:
            raise RequestError(
                f"coding tasks require an explicitly allowed model alias from {allowed}; "
                f"requested {selected_alias or 'the default'}"
            )
    model, error = resolve_model(dispatch, request.model_alias)
    if error or not model:
        raise RequestError(error or "model resolution failed")
    max_bytes = int((dispatch.get("execution") or {}).get("max_prompt_bytes", 50000))
    size = len(request.instructions.encode("utf-8", errors="replace"))
    if size > max_bytes:
        raise RequestError(f"task instructions are too large ({size} bytes; limit {max_bytes})")
    return ResolvedRequest(request=request, project=project, model=model)


def request_fingerprint(request: IssueRequest) -> str:
    return sha256_text(
        "|".join(
            [
                request.version,
                request.project.lower(),
                request.agent.lower(),
                (request.model_alias or "").lower(),
                request.session.lower(),
                request.task.lower(),
                request.instructions,
            ]
        )
    )[:24]
