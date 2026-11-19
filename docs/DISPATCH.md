# Dispatcher (v1.1)

The dispatcher is an optional process on the user's own Linux machine or VPS. It polls a private GitHub Issue inbox, validates a versioned request, runs OpenCode in one explicitly configured project workspace, captures the result through Agent Handoff, verifies the Drive upload, and publishes the direct report URL back to the originating issue.

It requires no inbound port and no public HTTP service. Core report capture works when the dispatcher is disabled.

## Scope

Implemented:

- GitHub Issues transport via the user's authenticated `gh` session.
- Explicit project and model-alias resolution.
- `session: new` only.
- OpenCode execution adapter with read-only default policy.
- Durable execution/capture/upload/publication ledger.
- Restart recovery without rerunning an already claimed request.
- Upload verification plus a direct Drive file URL and `UPLOAD-RECEIPT.json`.
- Idempotent issue publication.

Not implemented in v1.1:

- AGY dispatch.
- Claude Code, Gemini CLI, Codex CLI, or other execution adapters.
- SAME SESSION continuation. It is rejected unless a safe continuation implementation exists.
- Automatic project inference or model selection.
- A dashboard, database server, public API, SaaS hosting, or multi-user service.

## Trust model

1. The repository is private and has Issues enabled.
2. Only logins in `dispatch.allowed_users` may submit tasks, and the authenticated `gh` user must also be in that list.
3. The Issue body is untrusted data. It can select only a configured project alias and model alias.
4. Paths, commands, tokens, and arbitrary model IDs are never accepted from Issue content.
5. The execution workspace must exactly match a path recorded for the Agent Handoff project.
6. Read-only execution is the default. Write mode requires both `permission_profile: workspace_write` and `allow_write: true`.
7. A request is claimed before execution and is never automatically executed again after a crash or restart.

## Configuration

Add a `dispatch` section to `$AGENT_HANDOFF_HOME/config/config.json`. Never copy an example over an existing runtime config.

```json
{
  "dispatch": {
    "enabled": true,
    "transport": "github_issues",
    "repository": "example-user/private-dispatch",
    "allowed_users": ["example-user"],
    "agent": "opencode",
    "models": {
      "fast": "provider/model-fast",
      "review": "provider/model-review"
    },
    "default_model": "fast",
    "projects": {
      "ProjectA": {"workspace": "/home/dev/projects/project-a"},
      "ProjectB": {"workspace": "/home/dev/projects/project-b"}
    },
    "poll_interval_seconds": 30,
    "execution": {
      "timeout_seconds": 1200,
      "max_prompt_bytes": 50000,
      "permission_profile": "read_only",
      "allow_write": false
    },
    "delivery": {"retry_seconds": 60, "max_attempts": 0},
    "github": {"command": "gh", "require_private": true}
  }
}
```

Fields:

| Field | Meaning |
| --- | --- |
| `enabled` | Must be `true` to run. Defaults to disabled when absent. |
| `transport` | Only `github_issues` is supported. |
| `repository` | Private `owner/name` repository with Issues enabled. |
| `allowed_users` | GitHub logins allowed to submit tasks. No wildcard. |
| `agent` | Only `opencode`. |
| `models` | Trusted alias → exact `provider/model` mapping. |
| `default_model` | Optional alias used when an issue omits `model`. |
| `projects` | Dispatch project name → `{ "workspace": "<canonical absolute path>" }`. The name must match an existing core project; the workspace must exactly match one of that project's recorded paths. |
| `poll_interval_seconds` | Watch-mode polling interval. |
| `execution.timeout_seconds` | Bounded OpenCode execution timeout. |
| `execution.max_prompt_bytes` | Maximum Issue instruction size. |
| `execution.permission_profile` | `read_only` (default) or `workspace_write`. |
| `execution.allow_write` | Must be explicitly true with `workspace_write`. |
| `delivery.retry_seconds` | Base backoff for GitHub publication retries. |
| `delivery.max_attempts` | `0` means retry indefinitely. |
| `github.command` | `gh` executable name or absolute path. |
| `github.require_private` | Must remain `true`. |

Discover exact OpenCode model IDs:

```bash
opencode models
```

The tool does not infer model IDs and does not accept them from Issue content.

## Issue contract

Use `.github/ISSUE_TEMPLATE/agent-handoff-task.md` or this exact form:

```text
<!-- agent-handoff-request -->
handoff-version: 1
project: ProjectA
agent: opencode
model: fast
session: new
<!-- /agent-handoff-request -->

Read the first heading of README.md and report it. Do not edit files.
```

Rules:

- `handoff-version` must be `1`.
- `project` must match a configured dispatch project or its core Agent Handoff project name/key.
- `agent` must be `opencode` and must match `dispatch.agent`.
- `model` is an alias from `dispatch.models`; it may be omitted only if `default_model` is configured.
- `session` must be `new`. `same` is rejected with an explicit unsupported message.
- Unknown or duplicate metadata fields are rejected.
- Instructions must be non-empty and within `execution.max_prompt_bytes`.
- Bodies without the request marker are ignored; they are not treated as requests.
- Issue titles and labels are not routing inputs.

A malformed request from an authorized user receives one rejection comment (marker-idempotent). An unauthorized author is recorded as rejected without execution.

## State machine

Execution, capture, upload, and publication are tracked separately.

```text
NEW
  |
  v
CLAIMED
  |
  v
RUNNING
  |
  v
EXECUTION_COMPLETED
  |
  v
REPORT_CAPTURED / UPLOAD_PENDING
  |
  v
UPLOAD_VERIFIED
  |
  v
ISSUE_UPDATED

Terminal/exceptional:
  REJECTED          malformed or unauthorized request
  NEEDS_ATTENTION   uncertain execution with no recoverable output
```

Durable records live under `$AGENT_HANDOFF_HOME/state/dispatch/`:

```text
state/dispatch/
  ledger.lock
  runner.lock
  records/<hash>.json
  sessions/<hash>-<timestamp>.jsonl
```

Each record preserves repository, issue number, project, agent, model, session ID, Agent Handoff task/run IDs, execution status and outcome, capture status, upload status, Drive report identity, receipt identity, publication status, attempts, backoff, and a bounded history.

Writes are atomic; ledger mutations use `flock`. Only one dispatcher runner lock is active at a time. The ledger lock is never held during model execution.

## Crash and restart behavior

| Crash point | Behavior after restart |
| --- | --- |
| Before claim persistence | The request was never claimed; normal polling may claim it. |
| After claim, before execution start | `NEEDS_ATTENTION`. The model is **not** rerun. |
| During execution, session ID persisted | Raw OpenCode events are replayed from `sessions/*.jsonl`; the report is captured without rerunning the model. |
| During execution, no recoverable output | `NEEDS_ATTENTION`. The model is **not** rerun. |
| After execution, before report capture | Persisted events are used to create the missing report. |
| After capture, before upload | Upload retries with the existing task/run identity. |
| After verified upload, before publication | Publication retries without another upload or execution. |
| After comment posted, before ledger update | The delivery marker is found and publication is recorded without a duplicate comment. |

`dispatch deliver` reconciles an existing run manually and never invokes an agent.

## Upload verification and direct URLs

The core upload contract is unchanged:

- use `rclone copyto`/`mkdir`/`lsf`/`md5sum` or read-back;
- never use `rclone sync`;
- mark a run upload-verified only after every file passes verification.

After verification the dispatcher:

1. resolves the actual Drive file ID with `rclone lsjson --stat`;
2. writes a local `UPLOAD-RECEIPT.json`;
3. uploads and verifies the receipt;
4. updates local `COMPLETE.json` with the report URL, receipt path, and verified metadata;
5. re-uploads and verifies `COMPLETE.json`;
6. only then publishes the comment.

This corrects the historical failure mode where a remote `COMPLETE.json` still said `upload.status=pending` because it had been uploaded before local verification. The receipt and re-uploaded `COMPLETE.json` are the truthful reconciliation; the existence of a Drive folder is never treated as verification.

## Commands

```bash
# One poll, then exit
agent-handoff dispatch run --once --json

# Continuous polling (foreground; use a service manager for persistence)
agent-handoff dispatch run --watch

# Inspect durable records
agent-handoff dispatch status --json

# Read local + remote metadata for one run without changes
agent-handoff dispatch inspect --task <task-id> --run <run-id> --json

# Reconcile and publish an existing run; never runs an agent
agent-handoff dispatch deliver --task <task-id> --run <run-id> \
  --issue <number> --repository owner/private-dispatch --json
```

`dispatch run` refuses to start when `dispatch.enabled` is false. `config validate` validates core and dispatch configuration without contacting GitHub or Drive.

## systemd user service (optional)

Do not enable a persistent service during installation or tests. Use a user service only with explicit consent.

`~/.config/systemd/user/agent-handoff-dispatch.service`:

```ini
[Unit]
Description=Agent Handoff private GitHub Issue dispatcher
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/agent-handoff dispatch run --watch
Environment=AGENT_HANDOFF_HOME=%h/agent-handoff
Restart=on-failure
RestartSec=30
WorkingDirectory=%h

[Install]
WantedBy=default.target
```

```bash
mkdir -p ~/.config/systemd/user
# write the unit file with your own paths
systemctl --user daemon-reload
systemctl --user enable --now agent-handoff-dispatch.service
journalctl --user -u agent-handoff-dispatch.service -f
```

The service runs as the user who owns `gh` and rclone credentials. It does not require root and does not open a network port.

## Failure recovery

```bash
agent-handoff dispatch status --json
```

- `UPLOAD_PENDING`: Drive unavailable or verification failed. Retry upload only.
- `UPLOAD_VERIFIED`: publication pending/failed. Retry comment only.
- `NEEDS_ATTENTION`: an execution was uncertain and no recoverable output exists. Inspect the host and session evidence; do not blindly rerun. Submit a new issue if the work must be repeated.
- `REJECTED`: malformed or unauthorized request. A new, corrected issue is required; editing an old rejected issue does not automatically execute it.

Backoff is automatic. `--force` ignores upload/publication backoff for an operator-controlled retry.

## Tests

Deterministic tests use fake GitHub, fake OpenCode, and fake rclone. They cover:

- valid request executes once;
- duplicate polling does not repeat execution;
- unauthorized/malformed/unknown project/unknown model/SAME SESSION rejection;
- project and session isolation;
- cwd/PWD correctness;
- execution → task/run attribution;
- upload retry without model restart;
- publication retry without duplicate links;
- restart after claim and after verified upload;
- stale remote `COMPLETE.json` reconciliation and receipts;
- core capture with dispatch disabled;
- configuration preservation across install/upgrade/rollback.

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Live acceptance requires real `gh`, OpenCode, and Drive credentials. If any are unavailable, report the limitation; do not invent a pass.
