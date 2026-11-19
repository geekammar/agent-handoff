#!/usr/bin/env python3
"""Run read-only OpenCode requests submitted through the private handoff repository."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = "geekammar/agent-handoff"
OWNER_ID = 194697160
PREFIX = "[handoff:opencode:Rabet:plan] "
CLAIM = "<!-- handoff-opencode-claimed-v1 -->"
STATE = Path.home() / ".local/state/agent-handoff-dispatch"


def command(arguments, **options):
    return subprocess.run(
        arguments, check=True, text=True, capture_output=True,
        stdin=subprocess.DEVNULL, **options,
    ).stdout


def github(endpoint):
    return json.loads(command(["gh", "api", endpoint], timeout=30))


def comment(number, message):
    command(["gh", "issue", "comment", str(number), "-R", REPO, "--body", message], timeout=30)


def from_owner(entry):
    return (entry.get("user") or {}).get("id") == OWNER_ID


def verify_inbox():
    info = github(f"repos/{REPO}")
    if not info.get("private") or not info.get("has_issues"):
        raise RuntimeError("The dispatch repository must be private with Issues enabled.")
    if github("user").get("id") != OWNER_ID:
        raise RuntimeError("Sign in to gh with the owner account before starting the dispatcher.")


def wait_for_report(title, issue_number, session_id, model):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        data = json.loads(command(
            ["agent-handoff", "list", "--project", "Rabet", "--json"], timeout=30,
        ))
        for row in data["rows"]:
            if row.get("title") != title or row.get("upload") != "verified":
                continue
            complete = json.loads(Path(row["report"]).with_name("COMPLETE.json").read_text())
            agent = complete.get("agent") or {}
            if agent.get("session_id") != session_id:
                continue
            if agent.get("model") != model:
                comment(issue_number, "The Drive report did not record the configured model. Check the report before accepting this run.")
                return
            report_path = Path(row["report"])
            relative = "/".join(("TASKS", *report_path.parts[-4:]))
            comment(issue_number, f"Drive upload verified. Outcome: `{row['outcome']}`.\n\n"
                                  f"Rabet report path: `{relative}`")
            return
        time.sleep(5)
    comment(issue_number, f"OpenCode session `{session_id}` returned. No matching verified Drive report was found within three minutes.")


def execute(issue, model, project):
    number = issue["number"]
    title = f"Agent Handoff GitHub issue {number}"
    prompt = issue.get("body") or ""
    if not 0 < len(prompt.strip()) <= 24000:
        comment(number, f"{CLAIM}\nRejected: the prompt must contain 1 to 24,000 characters.")
        return
    comment(number, f"{CLAIM}\nStarting a new read-only OpenCode session with `{model}`.")
    environment = dict(os.environ, OPENCODE_CONFIG_CONTENT=json.dumps({
        "share": "disabled",
        "permission": {
            "*": "deny",
            "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "**/.env*": "deny"},
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
        },
    }))
    try:
        output = command(
            ["opencode", "run", "--agent", "plan", "--model", model, "--title", title,
             "--format", "json", "Task submitted through Agent Handoff:\n\n" + prompt],
            cwd=project, env=environment, timeout=1200,
        )
        session_ids = set()
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("sessionID"):
                session_ids.add(event["sessionID"])
        if len(session_ids) != 1:
            comment(number, "OpenCode output did not identify a unique session. Check the VPS before retrying.")
            return
        wait_for_report(title, number, session_ids.pop(), model)
    except (OSError, ValueError, subprocess.SubprocessError):
        comment(number, "The dispatch or report check failed. Check the VPS before retrying.")


def poll(model, project):
    verify_inbox()
    issues = json.loads(command(
        ["gh", "api", "--paginate", "--slurp",
         f"repos/{REPO}/issues?state=open&sort=created&direction=asc&per_page=100"],
        timeout=45,
    ))
    for page in issues:
        for issue in page:
            if "pull_request" in issue or not from_owner(issue) or not issue["title"].startswith(PREFIX):
                continue
            number = issue["number"]
            comments = json.loads(command(
                ["gh", "api", "--paginate", "--slurp", f"repos/{REPO}/issues/{number}/comments"],
                timeout=45,
            ))
            if any(from_owner(entry) and entry["body"].startswith(CLAIM) for page in comments for entry in page):
                continue
            execute(issue, model, project)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact DeepSeek Flash provider/model ID from opencode models")
    parser.add_argument("--project", type=Path, required=True, help="Local Rabet repository mapped in Agent Handoff")
    parser.add_argument("--watch", action="store_true", help="Poll for new requests every 20 seconds")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w./:-]*deepseek[\w./:-]*flash[\w./:-]*", args.model, re.IGNORECASE):
        parser.error("--model must identify a DeepSeek Flash model")
    if not args.project.is_dir():
        parser.error("--project must be an existing Rabet directory")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / "rabet.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A Rabet dispatcher is already running under this user")
        print(f"Listening for Rabet requests in {REPO} with {args.model}.", flush=True)
        while True:
            try:
                poll(args.model, args.project)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"Dispatch check failed ({type(error).__name__}). Check gh, OpenCode, and Agent Handoff.", file=sys.stderr)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(20)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
