---
name: agent-handoff
description: Install, configure, operate, and verify Agent Handoff, a portable report-capture tool with an optional private-GitHub-Issue dispatcher for OpenCode.
---

# Agent Handoff skill

This file is reusable guidance for any assistant that supports a skill-style entrypoint. It is also plain Markdown for assistants that do not. The canonical, detailed sources remain `README.md`, `AGENTS.md`, and `docs/DISPATCH.md`.

## Use this skill when

- A user wants upload-verified reports for OpenCode or AGY work.
- A user wants to install Agent Handoff on their own Linux machine or VPS.
- A user wants a private GitHub Issue to run OpenCode in one explicit local project.
- A user needs to diagnose a pending report upload, a stale remote `COMPLETE.json`, or a missing issue delivery.

## Do not use this skill to

- install software on a machine where you have no shell access;
- pretend to authenticate GitHub or Google Drive without user authorization;
- implement or test AGY dispatch;
- simulate SAME SESSION continuation;
- add a web service, database server, container platform, or hosted SaaS;
- modify unrelated application source code.

## Capability honesty

| You have | You can |
| --- | --- |
| Repository text only | Explain prerequisites, commands, and safety checks. |
| Shell access | Clone, install, validate, run tests, and inspect local state. |
| Shell + `gh` auth | Verify the private issue inbox and deliver report links. |
| Shell + rclone Drive auth | Upload and verify reports. |
| No shell/GitHub/Drive access | Ask the user to run each command and paste only non-secret output. |

Never ask for tokens, rclone config contents, `.env` contents, or SSH private keys in chat.

## Canonical install path

```bash
git clone https://github.com/OWNER/agent-handoff.git ~/src/agent-handoff
python3 ~/src/agent-handoff/setup/install.py --home ~/agent-handoff
agent-handoff auth
agent-handoff project add --name ProjectA --path ~/projects/project-a
agent-handoff project add --name ProjectB --path ~/projects/project-b
agent-handoff doctor
agent-handoff config validate
```

Use `--home` and `--bin-link` for non-standard locations. Never overwrite an existing runtime config with an example file.

## Verify before claiming success

```bash
agent-handoff --version
agent-handoff doctor --json
agent-handoff config validate --json
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

For a report, require all of the following before saying delivery succeeded:

1. local `REPORT.md` and `COMPLETE.json` exist;
2. `COMPLETE.json` has `upload.verified: true`;
3. the upload used MD5/read-back verification;
4. a direct `https://drive.google.com/file/d/.../view` URL was resolved;
5. an `UPLOAD-RECEIPT.json` exists locally and remotely;
6. the originating issue contains the delivery marker and URL.

A folder existing, a queue item disappearing, or `ok: true` from a retry command is not sufficient.

## Optional dispatcher

1. `gh auth status`
2. Confirm the repository is private with Issues enabled.
3. `opencode models` to discover exact model IDs.
4. Add `dispatch` to `$AGENT_HANDOFF_HOME/config/config.json` using `config/config.dispatch.example.json` as a structural reference only.
5. `agent-handoff config validate`
6. `agent-handoff dispatch run --once`
7. Continuous operation: a systemd **user** service with explicit user consent; see `docs/DISPATCH.md`.

Issue request format:

```text
<!-- agent-handoff-request -->
handoff-version: 1
project: ProjectA
agent: opencode
model: fast
session: new
<!-- /agent-handoff-request -->

Task instructions go here.
```

Reject `session: same`; v1.1 does not support continuation. Never derive a path or shell command from issue text.

## Recovery

```bash
agent-handoff dispatch status --json
agent-handoff dispatch inspect --task <task-id> --run <run-id> --json
agent-handoff dispatch deliver --task <task-id> --run <run-id> \
  --issue <number> --repository owner/private-dispatch --json
```

- Upload failure: retry upload only.
- GitHub publication failure: retry publication only.
- Crash after claim/start: never rerun automatically. Recover persisted session output if present; otherwise mark `NEEDS_ATTENTION`.
- Stale remote `COMPLETE.json`: run `dispatch deliver`; it re-uploads truthful verified metadata and a receipt without rerunning the agent.

## Boundaries

Keep the core CLI working when dispatch is disabled. Do not merge a Draft PR without human approval. Do not add speculative adapters. Stop and report accurately if a credential or permission is missing.
