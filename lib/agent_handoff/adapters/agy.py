from __future__ import annotations

import json
import os
from typing import Any

from ..util import first_line, sha256_text, strip_wrappers, utcnow_iso
from .common import auto_complete


def parse_agy_transcript(path: str | None) -> dict[str, Any]:
    users = []
    assistants = []
    if not path or not os.path.isfile(path):
        return {"users": users, "assistants": assistants}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                typ = item.get("type")
                content = item.get("content") or ""
                if typ == "USER_INPUT" and item.get("source") == "USER_EXPLICIT" and content:
                    users.append({"index": index, "content": strip_wrappers(content), "created_at": item.get("created_at")})
                elif typ == "PLANNER_RESPONSE" and item.get("status") == "DONE" and content:
                    assistants.append({"index": index, "content": content.strip(), "created_at": item.get("created_at")})
    except OSError:
        pass
    return {"users": users, "assistants": assistants}


def agy_outcome(reason: str | None, error: str | None, fully_idle: bool | None) -> str:
    reason = (reason or "").strip()
    error_l = (error or "").lower()
    if reason == "USER_CANCELED" or "cancel" in error_l or "abort" in error_l:
        return "cancelled"
    if reason == "ERROR" or (error or "").strip():
        return "failed"
    if reason == "NO_TOOL_CALL":
        return "succeeded" if fully_idle is not False else "blocked"
    if reason.startswith("MAX_"):
        return "blocked"
    if reason in ("TERMINAL_CUSTOM_HOOK", "INJECTED_RESPONSE", "TERMINAL_STEP_TYPE"):
        return "blocked"
    return "blocked"


def agy_hook(payload: dict[str, Any]) -> dict[str, Any]:
    if (payload.get("hookEventName") or "") != "Stop":
        return {"ok": True, "ignored": True, "reason": "not a Stop hook"}
    conversation_id = (payload.get("conversationId") or "").strip()
    if not conversation_id:
        return {"ok": False, "ignored": True, "reason": "missing conversationId"}
    cwd = (payload.get("cwd") or "").strip()
    if not cwd:
        roots = payload.get("workspacePaths") or []
        cwd = str(roots[0]) if roots else ""
    transcript_path = payload.get("transcriptPath")
    parsed = parse_agy_transcript(transcript_path)
    users = parsed["users"]
    assistants = parsed["assistants"]
    task_prompt = users[0]["content"] if users else (payload.get("lastUserInput") or "")
    run_prompt = users[-1]["content"] if users else (payload.get("lastUserInput") or "")
    last_user_index = users[-1]["index"] if users else -1
    summary = ""
    last_assistant_at = None
    for item in assistants:
        if item["index"] > last_user_index:
            summary = item["content"]
            last_assistant_at = item.get("created_at")
    reason = payload.get("terminationReason") or ""
    error = payload.get("error") or ""
    outcome = agy_outcome(reason, error, payload.get("fullyIdle"))
    transcript_size = 0
    transcript_mtime = 0
    if transcript_path and os.path.exists(transcript_path):
        try:
            stat = os.stat(transcript_path)
            transcript_size = stat.st_size
            transcript_mtime = int(stat.st_mtime)
        except OSError:
            pass
    event_key = sha256_text(
        "|".join(
            [
                conversation_id,
                str(payload.get("executionNum", "")),
                reason,
                (payload.get("lastUserInput") or ""),
                str(transcript_size),
                str(transcript_mtime),
                error,
                sha256_text(summary)[:16],
            ]
        )
    )[:32]
    title = first_line(task_prompt or run_prompt or summary or f"AGY conversation {conversation_id}")
    result = auto_complete(
        {
            "agent": "agy",
            "session_id": conversation_id,
            "cwd": cwd,
            "title": title,
            "task_prompt": task_prompt,
            "run_prompt": run_prompt,
            "summary": summary,
            "outcome": outcome,
            "error": error,
            "termination_reason": reason,
            "model": payload.get("modelName"),
            "event_key": event_key,
            "completed_at": last_assistant_at or utcnow_iso(),
            "tests": {"status": "not_verified", "details": "AGY Stop hook captured the session; no independent test evidence."},
            "deployment": {"status": "not_verified", "details": "AGY Stop hook captured the session; no independent deployment evidence."},
        }
    )
    return result
