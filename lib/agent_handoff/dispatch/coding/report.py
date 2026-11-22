from __future__ import annotations

from typing import Any, Mapping

from ...util import sanitize_error, truncate
from .milestones import milestone_state


def coding_outcome(record: Mapping[str, Any]) -> str:
    coding = record.get("coding") or {}
    if coding.get("blocked"):
        return f"blocked:{coding.get('blocked_stage') or 'unknown'}"
    publication = coding.get("publication") or {}
    if publication.get("pr_url"):
        return "draft_pr_published"
    if (coding.get("tests") or {}).get("status") == "passed":
        return "tests_passed"
    if (coding.get("agent") or {}).get("status") == "completed":
        return "agent_completed"
    return "incomplete"


def coding_next_action(record: Mapping[str, Any]) -> str:
    coding = record.get("coding") or {}
    publication = coding.get("publication") or {}
    upload = record.get("upload") or {}
    if coding.get("blocked"):
        return (
            "Inspect the preserved task evidence. Correct the trusted configuration, test profile, "
            "or requested change, then submit a new Issue. The agent is never re-run automatically."
        )
    if upload.get("verified"):
        return "Review the Draft Pull Request and the verified report; a human owner must merge."
    if publication.get("pr_url"):
        return "Wait for report upload verification. The Draft PR already exists and will not be replaced."
    if publication.get("next_attempt_at"):
        return f"Wait for the bounded publication retry at {publication.get('next_attempt_at')}."
    return "Inspect the durable coding stage; no automatic agent re-execution is allowed."


def coding_report_sections(record: Mapping[str, Any]) -> str:
    coding = record.get("coding") or {}
    publication = coding.get("publication") or {}
    tests = coding.get("tests") or {}
    diff = coding.get("diff") or {}
    agent = coding.get("agent") or {}
    limits = coding.get("limits") or {}
    request = record.get("request") or {}
    lines = [
        "",
        "## Coding task contract",
        "",
        f"- Task type: `coding`",
        f"- Repository: `{coding.get('repository') or 'not captured'}`",
        f"- Base branch: `{coding.get('base_branch') or 'not captured'}`",
        f"- Base commit: `{coding.get('base_commit') or 'not captured'}`",
        f"- Final commit: `{publication.get('commit') or 'not captured'}`",
        f"- Task branch: `{publication.get('branch') or 'not captured'}`",
        f"- Draft PR: {publication.get('pr_url') or 'not created'}",
        f"- Coding outcome: `{coding_outcome(record)}`",
        "",
        "### Request",
        "",
        truncate(str(request.get("instructions") or "").strip(), 20000) or "Not captured.",
        "",
        "## Coding stage and milestones",
        "",
        "Milestone values are a snapshot captured when this report was generated. "
        "`pending` means the durable stage had not reached that step yet; `not_run` means "
        "the task was blocked before it. Later upload, Issue, and Telegram steps are recorded "
        "in the durable ledger after this report snapshot and are not rewritten here.",
        "",
        f"- Durable stage: `{coding.get('stage')}`",
        f"- Agent completed: `{milestone_state(coding, 'AGENT_COMPLETED')}`",
        f"- Trusted tests passed: `{milestone_state(coding, 'TESTS_PASSED')}`",
        f"- Diff validated: `{milestone_state(coding, 'DIFF_VALIDATED')}`",
        f"- Commit created: `{milestone_state(coding, 'COMMIT_CREATED')}`",
        f"- Branch published: `{milestone_state(coding, 'BRANCH_PUBLISHED')}`",
        f"- Draft PR created: `{milestone_state(coding, 'PR_CREATED')}`",
        f"- Report verified: `{milestone_state(coding, 'REPORT_VERIFIED')}`",
        f"- Issue delivered: `{milestone_state(coding, 'ISSUE_DELIVERED')}`",
        f"- Telegram sent: `{milestone_state(coding, 'TELEGRAM_SENT')}`",
        f"- Report delivered (legacy alias of Issue delivered): `{milestone_state(coding, 'REPORT_DELIVERED')}`",
        f"- Blocked: `{bool(coding.get('blocked'))}`",
        f"- Blocked stage: `{coding.get('blocked_stage') or 'none'}`",
        f"- Blocked reason: {sanitize_error(str(coding.get('blocked_reason') or 'none'), 1200)}",
        "",
        "## Test execution evidence",
        "",
        f"- Profile: `{tests.get('profile') or coding.get('test_profile') or 'not captured'}`",
        f"- Status: `{tests.get('status') or 'not_run'}`",
        f"- Bounded timeout: `{tests.get('timeout_seconds')}` seconds",
        f"- Duration: `{tests.get('duration_seconds')}` seconds",
    ]
    for item in tests.get("commands") or []:
        argv = " ".join(str(part) for part in item.get("argv") or [])
        lines.append(
            f"- `{argv}` → `{item.get('status')}` exit `{item.get('exit_code')}` "
            f"duration `{item.get('duration_seconds')}s` truncated=`{item.get('output_truncated')}`"
        )
        stderr = str(item.get("stderr") or "").strip()
        if stderr and item.get("status") != "passed":
            lines.append("  - stderr: " + sanitize_error(stderr, 600).replace("\n", " "))
    if not tests.get("commands"):
        lines.append("- No trusted test command was executed.")
    lines.extend(
        [
            "",
            "## Diff validation evidence",
            "",
            f"- Status: `{diff.get('status') or 'not_run'}`",
            f"- Changed files: `{diff.get('changed_count')}`",
            f"- Diff bytes: `{diff.get('diff_bytes')}`",
            f"- Workspace bytes (excluding .git): `{diff.get('workspace_bytes')}`",
            f"- Secret scan enabled: `{diff.get('secret_scan')}`",
            f"- Secret findings (names only, values redacted): `{diff.get('secret_findings') or []}`",
            f"- Symlink findings: `{diff.get('symlinks') or []}`",
            f"- Submodule findings: `{diff.get('submodules') or []}`",
        ]
    )
    files = diff.get("files") or []
    for item in files[:100]:
        lines.append(f"- `{item.get('path')}` ({item.get('status')}, {item.get('size')} bytes)")
    if len(files) > 100:
        lines.append(f"- … and {len(files) - 100} more file(s)")
    blocked = diff.get("blocked") or []
    for reason in blocked[:50]:
        lines.append(f"- Blocked: {reason}")
    lines.extend(
        [
            "",
            "## Publication and retry evidence",
            "",
            f"- Publication status: `{publication.get('status') or 'not_started'}`",
            f"- Branch pushed: `{bool(publication.get('branch_pushed'))}`",
            f"- PR number: `{publication.get('pr_number')}`",
            f"- Draft: `{publication.get('draft')}`",
            f"- Publication attempts: `{publication.get('attempts', 0)}`",
            f"- Last publication error: {sanitize_error(str(publication.get('last_error') or 'none'), 800)}",
            f"- Next publication retry: `{publication.get('next_attempt_at') or 'none'}`",
            f"- Recovery attempts: `{(coding.get('recovery') or {}).get('attempts', 0)}`",
            f"- Task artifacts preserved: `{bool(coding.get('artifacts_preserved'))}`",
            "",
            "## Security restrictions and limits",
            "",
            "- The agent ran under the dedicated non-root Unix identity with an allowlisted environment.",
            "- The child did not receive parent GitHub, Drive, Telegram, or rclone credentials.",
            "- Trusted tests used the same isolated identity and a trusted, owner-configured argv profile.",
            "- Issue text cannot select commands, identity, repository, base branch, tests, or permissions.",
            "- Repository hooks and parent Git credentials were not used in the child workspace.",
            "- Unix UID/GID isolation is not a kernel network sandbox; tests may have network access.",
            "- Secret scanning is defense in depth and cannot prove the absence of sensitive data.",
            f"- File count limit: `{limits.get('max_files_changed')}`; diff byte limit: `{limits.get('max_diff_bytes')}`.",
            f"- Test timeout limit: `{limits.get('test_timeout_seconds')}s`; output limit: `{limits.get('max_output_bytes')}` bytes.",
            f"- Publication attempt limit: `{limits.get('max_publication_attempts')}`.",
            "",
            "## Unverified claims",
            "",
            "- The agent response is not independent proof that the implementation is correct or complete.",
            "- Tests passing does not prove security, performance, or production behavior.",
            "- No deployment was performed by C2.",
            "- No human code review or merge approval was performed.",
            "",
            "## Final outcome and next action",
            "",
            f"- Final coding outcome: `{coding_outcome(record)}`.",
            f"- Next action: {coding_next_action(record)}",
            "",
        ]
    )
    return "\n".join(lines)
