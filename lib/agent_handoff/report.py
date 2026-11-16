from __future__ import annotations

import re
from typing import Any

from .util import truncate

REQUIRED_SECTIONS = [
    "Executive Summary",
    "Task ID and Run ID",
    "Agent and Session",
    "Implemented Changes",
    "Fixed Issues",
    "Tests Passed",
    "Tests Failed",
    "Tests Not Run",
    "Deployment Status",
    "GitHub Branch, Commit and PR",
    "Remaining Issues",
    "Blockers",
    "Recommended Next Steps",
    "Architecture Changes",
    "Evidence links",
]


def _has_section(text: str, title: str) -> bool:
    pattern = re.compile(r"^\s{0,3}#{1,6}\s+" + re.escape(title) + r"\s*$", re.I | re.M)
    return bool(pattern.search(text))


def ensure_sections(text: str, defaults: dict[str, str] | None = None) -> str:
    defaults = defaults or {}
    result = (text or "").strip()
    if not result:
        result = "# Agent Execution Report\n"
    missing = []
    for title in REQUIRED_SECTIONS:
        if not _has_section(result, title):
            missing.append(title)
            result += f"\n\n## {title}\n\n{defaults.get(title, 'Not provided.')}\n"
    if missing:
        result = result.rstrip() + "\n"
    return result


def github_commit_url(repository: str | None, commit: str | None) -> str | None:
    if not repository or not commit:
        return None
    repo = repository.strip()
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo[len("git@github.com:") :]
    elif repo.startswith("ssh://git@github.com/"):
        repo = "https://github.com/" + repo[len("ssh://git@github.com/") :]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo.startswith("https://github.com/"):
        return None
    return f"{repo}/commit/{commit}"


def verification_notes(agent_outcome: str, tests: dict[str, Any], deployment: dict[str, Any], auto_generated: bool) -> str:
    lines = []
    lines.append(f"- Agent task outcome recorded by the reporting system: **{agent_outcome}**. This is not proof that tests, deployment, or the change itself succeeded.")
    if auto_generated:
        lines.append("- This report was generated automatically from session metadata and the final assistant response. It does not independently inspect the repository diff.")
    tests_status = (tests or {}).get("status", "not_verified")
    deployment_status = (deployment or {}).get("status", "not_verified")
    lines.append(f"- Test status: **{tests_status}**.")
    lines.append(f"- Deployment status: **{deployment_status}**.")
    if auto_generated:
        lines.append("- Verified: task/run identifiers, timestamps, agent/session metadata, and the final assistant response captured by the reporting system.")
    else:
        lines.append("- Verified: task/run identifiers, timestamps, agent/session metadata, and the submitted report file.")
    lines.append("- Unverified unless explicitly stated with evidence: implemented changes, fixes, test results, deployment results, and production behavior.")
    return "\n".join(lines)


def auto_report(
    *,
    task_id: str,
    run_id: str,
    agent: str,
    session_id: str | None,
    model: str | None,
    outcome: str,
    summary: str,
    repository: str | None,
    branch: str | None,
    commit: str | None,
    pr_url: str | None,
    tests: dict[str, Any],
    deployment: dict[str, Any],
    error: str | None,
    termination_reason: str | None,
    remaining_issues: str | None,
    blockers: str | None,
    run_dir: str,
    remote_report_path: str | None = None,
) -> str:
    summary_text = truncate((summary or "").strip(), 50000) or "No final assistant response was captured."
    error_text = (error or "").strip()
    terminal_text = (termination_reason or "").strip()
    tests_status = (tests or {}).get("status", "not_verified")
    tests_details = (tests or {}).get("details") or ""
    deployment_status = (deployment or {}).get("status", "not_verified")
    deployment_details = (deployment or {}).get("details") or ""
    lines = []
    lines.append(f"# Agent Execution Report — {task_id}")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(summary_text)
    lines.append("")
    lines.append("## Verification Notes")
    lines.append("")
    lines.append(verification_notes(outcome, tests, deployment, auto_generated=True))
    lines.append("")
    lines.append("## Task ID and Run ID")
    lines.append("")
    lines.append(f"- Task ID: `{task_id}`")
    lines.append(f"- Run ID: `{run_id}`")
    lines.append("")
    lines.append("## Agent and Session")
    lines.append("")
    lines.append(f"- Agent: `{agent}`")
    lines.append(f"- Session ID: `{session_id or 'not available'}`")
    lines.append(f"- Model: `{model or 'not captured'}`")
    lines.append(f"- Termination reason: `{terminal_text or 'not captured'}`")
    lines.append("")
    lines.append("## Implemented Changes")
    lines.append("")
    lines.append("Not independently verified. See the agent response in the Executive Summary and inspect the repository diff for the actual change set.")
    lines.append("")
    lines.append("## Fixed Issues")
    lines.append("")
    lines.append("Not independently verified. The reporting system did not inspect issue trackers or the diff.")
    lines.append("")
    lines.append("## Tests Passed")
    lines.append("")
    if tests_status == "passed":
        lines.append("Agent reported tests passed. Independent evidence was not captured by the reporting system.")
    else:
        lines.append("None verified.")
    if tests_details and tests_status == "passed":
        lines.append("")
        lines.append(tests_details)
    lines.append("")
    lines.append("## Tests Failed")
    lines.append("")
    if tests_status == "failed":
        lines.append("Tests failed according to the supplied completion data.")
        if tests_details:
            lines.append("")
            lines.append(tests_details)
    else:
        lines.append("None recorded.")
    lines.append("")
    lines.append("## Tests Not Run")
    lines.append("")
    if tests_status in ("not_verified", "not_run", "partial"):
        lines.append("Test execution was not fully and independently verified by the reporting system.")
        if tests_details:
            lines.append("")
            lines.append(tests_details)
    else:
        lines.append("No unrun test suite was reported.")
    lines.append("")
    lines.append("## Deployment Status")
    lines.append("")
    if deployment_status == "deployed":
        lines.append("A deployment was reported. Independent deployment verification was not performed by the reporting system.")
    elif deployment_status == "failed":
        lines.append("Deployment failed according to the supplied completion data.")
    elif deployment_status == "not_applicable":
        lines.append("Not applicable according to the supplied completion data.")
    else:
        lines.append("Not verified. No deployment evidence was captured.")
    if deployment_details:
        lines.append("")
        lines.append(deployment_details)
    lines.append("")
    lines.append("## GitHub Branch, Commit and PR")
    lines.append("")
    lines.append(f"- Repository: `{repository or 'not captured'}`")
    lines.append(f"- Branch: `{branch or 'not captured'}`")
    lines.append(f"- Commit: `{commit or 'not captured'}`")
    lines.append(f"- PR: {pr_url or 'not captured'}")
    lines.append("")
    lines.append("## Remaining Issues")
    lines.append("")
    lines.append((remaining_issues or "").strip() or "None reported.")
    lines.append("")
    lines.append("## Blockers")
    lines.append("")
    blocker_text = (blockers or "").strip()
    if error_text:
        blocker_text = (blocker_text + "\n\n" if blocker_text else "") + f"Reported error: `{error_text}`"
    lines.append(blocker_text or "None reported.")
    lines.append("")
    lines.append("## Recommended Next Steps")
    lines.append("")
    lines.append("- Review the repository diff and the agent response above.")
    lines.append("- Run the relevant test suite and record the exact command and result.")
    lines.append("- Confirm whether a deployment occurred and verify the deployed revision.")
    lines.append("- Update this report or add a follow-up task if anything remains open.")
    lines.append("")
    lines.append("## Architecture Changes")
    lines.append("")
    lines.append("Not reported by the agent and not independently detected by the reporting system.")
    lines.append("")
    lines.append("## Evidence links")
    lines.append("")
    lines.append(f"- Local run directory: `{run_dir}`")
    if remote_report_path:
        lines.append(f"- Drive report: `{remote_report_path}`")
    url = github_commit_url(repository, commit)
    if url:
        lines.append(f"- Commit: {url}")
    if pr_url:
        lines.append(f"- Pull request: {pr_url}")
    lines.append("")
    return "\n".join(lines)


def manual_report(
    *,
    task_id: str,
    run_id: str,
    agent: str,
    session_id: str | None,
    model: str | None,
    outcome: str,
    report_text: str,
    repository: str | None,
    branch: str | None,
    commit: str | None,
    pr_url: str | None,
    tests: dict[str, Any],
    deployment: dict[str, Any],
    remaining_issues: str | None,
    blockers: str | None,
    run_dir: str,
    remote_report_path: str | None = None,
) -> str:
    defaults = {
        "Executive Summary": "Not provided by the agent.",
        "Task ID and Run ID": f"- Task ID: `{task_id}`\n- Run ID: `{run_id}`",
        "Agent and Session": f"- Agent: `{agent}`\n- Session ID: `{session_id or 'not available'}`\n- Model: `{model or 'not captured'}`",
        "Implemented Changes": "Not provided by the agent.",
        "Fixed Issues": "Not provided by the agent.",
        "Tests Passed": (
            "Reported as passed by completion metadata; the reporting system did not independently run the tests."
            if (tests or {}).get("status") == "passed"
            else "Not provided by the agent."
        ),
        "Tests Failed": (
            "Reported as failed by completion metadata."
            if (tests or {}).get("status") == "failed"
            else "Not provided by the agent."
        ),
        "Tests Not Run": (
            "Test execution was not fully verified by the reporting system."
            if (tests or {}).get("status") in ("not_run", "not_verified", "partial")
            else "Not provided by the agent."
        ),
        "Deployment Status": (
            "Deployment was reported; the reporting system did not independently verify it."
            if (deployment or {}).get("status") == "deployed"
            else "Deployment failed according to completion metadata."
            if (deployment or {}).get("status") == "failed"
            else "Not applicable according to completion metadata."
            if (deployment or {}).get("status") == "not_applicable"
            else "Not verified. No deployment evidence was captured."
        ),
        "GitHub Branch, Commit and PR": f"- Repository: `{repository or 'not captured'}`\n- Branch: `{branch or 'not captured'}`\n- Commit: `{commit or 'not captured'}`\n- PR: {pr_url or 'not captured'}",
        "Remaining Issues": (remaining_issues or "").strip() or "None reported.",
        "Blockers": (blockers or "").strip() or "None reported.",
        "Recommended Next Steps": "Review the report and repository diff, then run and record the relevant tests.",
        "Architecture Changes": "Not provided by the agent.",
        "Evidence links": f"- Local run directory: `{run_dir}`" + (f"\n- Drive report: `{remote_report_path}`" if remote_report_path else ""),
    }
    result = ensure_sections(report_text, defaults)
    header = [
        f"# Agent Execution Report — {task_id}",
        "",
        "## Verification Notes",
        "",
        verification_notes(outcome, tests, deployment, auto_generated=False),
        "",
    ]
    if _has_section(result, "Verification Notes"):
        return result
    result = re.sub(r"^\s*#\s+.*?\n", "", result, count=1) if result.lstrip().startswith("# ") else result
    return "\n".join(header) + "\n" + result.lstrip() + ("\n" if not result.endswith("\n") else "")
