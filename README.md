# Agent Handoff

Agent Handoff is an AI-native, self-describing, portable developer tool for recording agent work and delivering upload-verified execution reports.

It has these layers:

1. **Core capture (always available):** records structured reports for OpenCode and AGY sessions, stores them locally, uploads them to a Google Drive reports subtree with `rclone`, and verifies the upload using MD5 or read-back.
2. **Optional GitHub Issue dispatcher (v1.1):** a separate process on your own Linux machine claims explicitly configured tasks from a private GitHub Issue, runs a local OpenCode model in a fixed project workspace, captures the report through Agent Handoff, uploads it, verifies it, and publishes the direct Drive report URL back to the originating issue.
3. **Optional Telegram notifications (v1.2 P1):** after the durable dispatcher record reaches `ISSUE_UPDATED`, a small local outbox sends one concise Telegram message with the verified Drive report URL, the original Issue URL, and a copy-ready Arabic ChatGPT handoff prompt. Telegram failures never change report delivery.
4. **Opt-in secure coding execution foundation (C1):** per-project permission policy, a dedicated non-root child identity, an allowlisted child environment, dedicated HOME/XDG/model auth, and canonical clean-workspace validation. Coding-task and Draft PR automation is C2 and is not enabled in production.

The runtime is deliberately small: Python standard library, `rclone`, and (for the optional dispatcher) the GitHub CLI `gh` plus OpenCode. There is no database server, web dashboard, inbound public port, SaaS dependency, or mandatory cloud service.

**Every user owns their installation.** Nothing depends on the original author's GitHub account, Drive, VPS, project paths, or model provider.

## Quick start

Prerequisites: Python 3.10+, `rclone`, and (optional) OpenCode. The dispatcher additionally needs `gh` authenticated to your own GitHub account.

```bash
git clone https://github.com/OWNER/agent-handoff.git ~/src/agent-handoff
python3 ~/src/agent-handoff/setup/install.py --home ~/agent-handoff
agent-handoff auth
agent-handoff project add --name ProjectA --path ~/projects/project-a
agent-handoff doctor
```

For a non-standard runtime or PATH location:

```bash
AGENT_HANDOFF_HOME="$HOME/agent-handoff" \
  python3 ~/src/agent-handoff/setup/install.py \
  --home "$HOME/agent-handoff" \
  --bin-link "$HOME/.local/bin/agent-handoff"
```

The installer synchronizes managed source files, records an ownership manifest, links the CLI, and registers the OpenCode and AGY capture integrations. It never touches runtime `data/`, `queue/`, `state/`, `locks/`, `logs/`, `tmp/`, `releases/`, `config/config.json`, or `rclone` authorization.

## Architecture

```text
AI assistant
    |
    v
private GitHub Issue (optional dispatcher transport)
    |
    v
Agent Handoff dispatcher (separate, optional process)
    |
    +-- request validation + authorized author check
    +-- explicit project/model resolution
    +-- durable execution ledger
    |
    v
OpenCode adapter (only execution adapter in v1.1)
    |
    v
fixed project workspace (cwd and PWD are the canonical configured path)
    |
    v
Agent Handoff core
    |
    +-- local task/run report
    +-- upload queue
    +-- rclone upload + MD5/read-back verification
    +-- direct Google Drive file URL + upload receipt
    |
    v
idempotent GitHub Issue delivery reconciliation
    |
    v
verified report URL
```

Core capture works with the dispatcher disabled. The dispatcher never needs an inbound public port: it polls the private repository using your authenticated `gh` session.

## Supported agents

| Agent | Report capture | Dispatcher execution |
| --- | --- | --- |
| OpenCode | Yes (verified v1 integration) | Yes (v1.1 adapter) |
| AGY | Yes (verified Stop hook) | No, deferred |
| Claude Code, Gemini CLI, Codex CLI, other CLIs | No | No; the adapter boundary is documented but no speculative adapters ship |

## Google Drive authorization

Authorization is the only manual Drive step. The CLI never prints rclone secrets or token contents.

```bash
agent-handoff auth
agent-handoff auth --check
agent-handoff doctor
```

Headless-safe import:

1. On a browser-capable machine run `rclone authorize "drive"`.
2. Sign in with the account that owns the Drive subtree.
3. Save the JSON token as a mode-600 file on the target host. Do not paste it into chat.
4. Import it:

```bash
agent-handoff auth --import-token-file /path/to/gdrive-token.json
agent-handoff doctor
```

The import backs up the previous rclone config, writes only the configured remote, verifies Drive, and restores the previous config if verification fails.

## Core usage

```bash
agent-handoff doctor
agent-handoff list --project ProjectA
agent-handoff retry-upload
agent-handoff index --project ProjectA --upload
```

Automatic capture:

- **OpenCode:** the plugin captures the first prompt and final assistant response, then calls `agent-handoff auto-complete`.
- **AGY:** the global `Stop` hook parses the transcript and writes a task/run report.

Manual structured flow:

```bash
agent-handoff init --project ProjectA --agent opencode \
  --title "Fix login redirect" --prompt-file prompt.txt
agent-handoff prepare-run --task-id <task-id>
# write RUNS/<run-id>/REPORT.md
agent-handoff complete --task-id <task-id> --run-id <run-id> \
  --outcome succeeded --tests passed \
  --tests-details "exact command and result"
```

Every `REPORT.md` contains required sections including Executive Summary, Task ID and Run ID, Agent and Session, Implemented Changes, Tests, Deployment, GitHub branch/commit/PR, Remaining Issues, Blockers, and Evidence links. The reporting system does not inspect diffs or run tests itself; it only records what was actually supplied.

## Optional dispatcher

The dispatcher is disabled by default. Start from `config/config.dispatch.example.json` and adapt it to your own GitHub repository, project paths, Drive mapping, and OpenCode models.

```bash
# Discover exact provider/model IDs available to your OpenCode installation:
opencode models

# Example-only paths and values:
cp ~/src/agent-handoff/config/config.dispatch.example.json \
  "$AGENT_HANDOFF_HOME/config/config.json"   # only for a brand-new installation
agent-handoff config validate
agent-handoff dispatch run --once
```

The example is illustrative. Do not copy it over an existing runtime config. Add `dispatch` to your existing config as a new section and preserve every existing value.

Minimal dispatch section:

```json
{
  "dispatch": {
    "enabled": true,
    "transport": "github_issues",
    "repository": "example-user/private-dispatch",
    "allowed_users": ["example-user"],
    "agent": "opencode",
    "models": {
      "fast": "provider/model-name",
      "review": "provider/other-model"
    },
    "default_model": "fast",
    "projects": {
      "ProjectA": {"workspace": "/home/dev/projects/project-a"},
      "ProjectB": {"workspace": "/home/dev/projects/project-b"}
    },
    "execution": {
      "timeout_seconds": 1200,
      "permission_profile": "read_only",
      "allow_write": false
    }
  }
}
```

Rules enforced by the dispatcher:

- The repository must be private and have Issues enabled.
- Only logins in `allowed_users` may submit tasks.
- `ProjectA`/`ProjectB` must already exist in the core `projects` mapping with a Drive folder ID and a matching local path.
- Issue content selects only a configured project alias and model alias. It can never provide a filesystem path, shell command, or arbitrary model ID.
- Unknown projects, agents, models, malformed metadata, and duplicate execution attempts are rejected.
- `session: new` is the only supported mode in v1.1. `session: same` is rejected explicitly; it is never simulated.
- Read-only execution is the default. A write-capable mode requires an explicit `workspace_write` permission profile and `allow_write: true`. `allow_write` must be a JSON boolean, and a project-level `workspace_write` requires its own explicit `allow_write: true`; a missing project override inherits the global policy.

### Issue request format

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

Coding tasks add `task: coding` to the metadata block and require an explicit owner-configured coding policy. Coding is disabled by default; unknown task types fail closed. See [docs/SECURE-CODING.md](docs/SECURE-CODING.md).

The metadata block is versioned and parsed only from the Issue body. Free-form prose without the marker is ignored. Issue titles are not used for routing. A controlled template is included at `.github/ISSUE_TEMPLATE/agent-handoff-task.md`.

### Delivery guarantees and recovery

The durable ledger separates:

1. execution status,
2. report capture status,
3. upload verification status,
4. GitHub publication status.

A claimed or started request is never automatically executed again after a restart. If execution output is recoverable, the dispatcher captures and delivers it without invoking the model again. Otherwise the record becomes `NEEDS_ATTENTION` for operator review. Upload failures retry the upload only; GitHub failures retry the comment only. Existing delivery comments are detected by marker to avoid duplicates.

An upload is considered verified only after every uploaded file passes MD5 or read-back verification. The dispatcher then resolves the actual Drive file ID, writes an `UPLOAD-RECEIPT.json`, re-uploads `COMPLETE.json` with truthful verified metadata, and only then publishes the direct report URL. A stale remote `COMPLETE.json` from an older upload is corrected; it is never treated as proof of verification by itself.

```bash
agent-handoff dispatch run --once                 # interactive: progress on stderr
agent-handoff dispatch run --once --json          # stdout remains valid JSON
agent-handoff dispatch run --once --progress      # force progress on non-TTY stderr
agent-handoff dispatch status                     # health, links, next actions
agent-handoff dispatch inspect --task <task-id> --run <run-id>
agent-handoff dispatch deliver --task <task-id> --run <run-id> \
  --issue <number> --repository owner/private-dispatch
```

Progress reports discovered, validated, executing, captured, uploading, verifying, receipt reconciliation, publishing, and completion stages with elapsed time, plus a periodic heartbeat during long OpenCode execution and slow uploads or verification without inventing an ETA. It never prints Issue instructions, prompt text, tokens, or local workspace paths. `--quiet` suppresses human progress without changing JSON stdout. `dispatch deliver` reconciles an existing run and publishes its verified URL. It never invokes an agent.

See [docs/DISPATCH.md](docs/DISPATCH.md) for the state machine, trust model, exact commands, systemd user-service example, and recovery procedures.

## Optional isolated coding workflow (C2)

C2 is implemented in source but **disabled by default and not enabled in production**. It adds one explicit coding task type on top of the C1 isolation foundation:

- the owner configures the repository, base branch, disposable workspace root, publication staging root, model allowlist, trusted test profile, path rules, and limits;
- the dispatcher creates a fresh per-task clone at a verified base commit, owned by the dedicated non-root identity;
- OpenCode runs with `workspace_write` only inside that disposable workspace;
- trusted tests run with the same non-root identity, a bounded timeout, bounded output, and an allowlisted environment;
- the dispatcher validates changed paths, protected files, diff size, binary/symlink/submodule changes, and obvious secret material;
- one task branch is committed from validated files in a parent-owned staging repository and pushed without force;
- exactly one Draft Pull Request is created or recovered by a stable marker; after the report upload is verified, the verified report link is added to the PR body without creating a second PR;
- the existing verified report upload, GitHub Issue comment, and Telegram outbox follow.

C2 never merges, never pushes to the default branch, never deploys, and never reuses a dirty developer checkout. If any trusted precondition is missing, it fails closed and preserves evidence. See [docs/SECURE-CODING.md](docs/SECURE-CODING.md) for the contract, state machine, security limitations, and staging acceptance plan.

Coding task metadata adds only:

```text
task: coding
```

Issue text cannot set commands, tests, UID/GID, repository, base branch, credentials, protected paths, isolation, or a model outside the configured allowlist.

## Optional Telegram notifications

Telegram notifications are **disabled by default** and are independent of task execution and report delivery. A missing token, an invalid chat ID, a Telegram outage, or a failed send never changes a delivered report, reruns OpenCode, re-uploads the report, or reposts the GitHub comment.

Add a `notifications` section to the runtime config and keep only the environment variable **name** in JSON:

```json
{
  "notifications": {
    "telegram": {
      "enabled": true,
      "chat_id": "-1001234567890",
      "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
      "timeout_seconds": 10,
      "retry_seconds": 60,
      "max_attempts": 5,
      "delayed_after_attempts": 3
    }
  }
}
```

Export the actual bot token in the dispatcher service environment. Never put it in the repository, the runtime JSON file, an Issue, a report, or a chat message:

```bash
export AGENT_HANDOFF_TELEGRAM_TOKEN='<bot-token>'
agent-handoff config validate
agent-handoff dispatch run --once
```

Restart a running dispatcher service after changing notification configuration so the new settings and activation boundary take effect.

The owner must create the bot with Telegram's BotFather and obtain the recipient chat ID. The tool does not create bots, log in to Telegram, or alter Drive sharing. A private Drive report URL is usable only by a Telegram recipient who can already access that Drive file.

A successful delivery notification contains the project, the Issue title, the literal `Verified & Delivered` delivery status, the execution outcome, the direct Google Drive `REPORT.md` URL, the original Issue URL, and a short Arabic prompt:

```text
تم الانتهاء من مهمة ProjectA — Fix login redirect.
اقرأ تقرير التنفيذ:
https://drive.google.com/file/d/<verified-file-id>/view
وراجع الكود والاختبارات، ثم أخبرني بالنتيجة
والإصلاحات والمتبقي والخطوة التالية.
```

The prompt is a normal selectable text block. Inline `View Report` and `View GitHub Issue` buttons are provided; there is no native Telegram copy button. The prompt does not claim that ChatGPT has already read the report.

Notification kinds:

| Kind | When |
| --- | --- |
| `DELIVERY_COMPLETED` | Only after `ISSUE_UPDATED`, a verified upload, a verified upload receipt, and confirmed GitHub publication. |
| `NEEDS_ATTENTION` | Uncertain execution, capture failure requiring review, or exhausted GitHub publication retries. Never described as successful delivery. |
| `DELIVERY_DELAYED` | A captured task whose upload is still failing, or a verified task whose GitHub publication is still failing. It states which situation is current and never calls an unverified upload a verified report. Thresholded by `delayed_after_attempts`; it does not fire on every poll. |

Notifications are stored in a durable local outbox under `state/dispatch/notifications/`. A stable identity of project + task + run + kind prevents repeated completion messages on later polls. Pending notifications survive a dispatcher restart without rerunning OpenCode or republishing the Issue. Before a queued message is sent, its durable dispatch record is rechecked; if the task recovered or the attention condition was resolved, the obsolete notification becomes `SUPERSEDED` instead of being sent. The audit record is retained, not deleted. Telegram has no universal end-to-end exactly-once transaction with the local filesystem, so delivery is best-effort at-least-once with local deduplication; an interrupted send is flagged for review rather than silently resent.

Old already-delivered records are never backfilled. Reconciliation is bounded by the real lifecycle transition timestamp (for example `publication.published_at` for completion, or the recorded attention transition), not by a generic `updated_at` change. Enabling notifications therefore does not flood the owner with historical reports, while genuinely new transitions after enablement can still be recovered.

The Telegram client always targets the official `https://api.telegram.org` Bot API. It does not read a runtime environment variable for an alternate API host; deterministic tests inject a fake transport explicitly.

```bash
agent-handoff dispatch status                 # includes notification counts and per-task state
agent-handoff dispatch notify                 # inspect pending/failed notifications
agent-handoff dispatch notify --json
agent-handoff dispatch notify --retry         # retry confirmed failures only; never runs agents
agent-handoff dispatch notify --retry --task <task-id> --run <run-id> --json
agent-handoff dispatch notify --retry --notification <id> --acknowledge-duplicate --json
```

A generic `--retry` never resends an ambiguous interrupted send, because that could deliver a duplicate. After reviewing the Telegram chat, an operator may retry one specific notification with `--notification` and `--acknowledge-duplicate`.

See [docs/DISPATCH.md](docs/DISPATCH.md) for the outbox state machine and [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for notification recovery.

## AI-native onboarding

Share the repository URL with a tool-enabled AI assistant and point it at [AGENTS.md](AGENTS.md) or [docs/AI-QUICKSTART.md](docs/AI-QUICKSTART.md).

An assistant with shell, GitHub, and Drive authorization can follow the commands and verify each step. An assistant without those permissions must explain the setup steps and ask the user to run them; it must not pretend to have installed, authenticated, executed, uploaded, or verified anything.

A universal prompt is provided in `docs/AI-QUICKSTART.md`. Tool-specific instructions are thin pointers to the same canonical commands, not divergent copies.

## Tests

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Deterministic tests use fake GitHub, fake OpenCode, and fake rclone backends. They do not require Drive credentials or a real model. Live acceptance optionally checks real OpenCode/AGY sessions and the real Drive hierarchy:

```bash
cp tests/live-config.example.json tests/live-config.json
python3 tests/acceptance.py
```

See [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).

## Upgrade and rollback

```bash
git -C ~/src/agent-handoff pull --ff-only
python3 ~/src/agent-handoff/setup/upgrade.py --home ~/agent-handoff
python3 ~/src/agent-handoff/setup/upgrade.py --home ~/agent-handoff --rollback latest
python3 ~/src/agent-handoff/setup/upgrade.py --home ~/agent-handoff --list
```

Upgrade snapshots the installed code and restores it automatically if installation fails. Rollback uses ownership manifests and refuses unsafe or legacy snapshots. Runtime reports, dispatch ledger, queue, logs, configuration, and credentials are preserved.

## Security and privacy

- Credentials, rclone configuration, tokens, `.env` files, and OAuth data are never committed.
- The dispatcher repository must be private; only explicitly allowed authors are accepted.
- Issue text is untrusted input, never privileged configuration.
- The dispatcher never derives filesystem paths or shell commands from issue content.
- The default execution policy is read-only (`plan` agent, denied writes, denied env reads). This is an OpenCode tool-permission policy, not an OS-level sandbox; see the security limitations in `docs/DISPATCH.md`.
- C1 adds an opt-in, disabled-by-default isolated coding profile: a dedicated non-root UID/GID, empty supplementary groups, an allowlisted child environment, dedicated HOME/XDG/model auth, and canonical clean-workspace checks. The parent dispatcher still runs as root. This is UID/environment separation, not full container or kernel sandboxing, and no coding tasks are enabled in production; see `docs/SECURE-CODING.md`.
- Logs and uploaded reports are sanitized of obvious secrets; local report copies remain intact for audit.
- The Telegram bot token is read from an environment variable named in config. It is never written to the repository, reports, notification records, logs, or error messages.
- Telegram messages contain only project, task title, status/outcome, verified URLs, and a sanitized reason. They never include Issue instructions, prompts, transcripts, local paths, or credentials.
- Uploaded receipts contain identities and hashes, not local filesystem paths or credentials.
- Runtime config and data are mode `600`/`700` and intentionally outside Git.

## Repository layout

```text
agent-handoff/
  bin/agent-handoff
  lib/agent_handoff/
    cli.py, config.py, report.py, store.py, upload.py, util.py
    adapters/                  # core session-ingest adapters (OpenCode/AGY capture)
    dispatch/                  # optional v1.1 dispatcher
      config.py, requests.py, ledger.py, github.py, runner.py, delivery.py
      security.py              # C1 identity/environment/workspace validation
      adapters/base.py, adapters/opencode.py
    notify/                    # optional Telegram outbox and transport
      config.py, messages.py, service.py, store.py, transport.py
  integrations/                # installed capture integrations
  setup/                       # installer, upgrade, rollback, auth helper
  tests/                       # deterministic tests + fake backends
  config/                      # sanitized examples only
  docs/                        # architecture, dispatch, acceptance, troubleshooting, AI quickstart
  AGENTS.md                    # neutral AI entrypoint
  SKILL.md                     # reusable skill-oriented guide
```

## Known limitations

- SAME SESSION continuation is deferred and rejected explicitly.
- C1 isolation and C2 coding are opt-in and disabled in production. C2 implements disposable workspaces, trusted tests, diff validation, and Draft PR publication, but it has not been independently accepted or enabled for production coding tasks. Unix UID/GID separation is not a kernel/network sandbox.
- Only OpenCode execution is implemented. AGY remains capture-only. No other CLI adapters are shipped.
- No automatic model selection or AI-based project inference.
- Automatic reports cannot independently verify code changes, tests, or deployments.
- The rclone OAuth token can expire or be revoked.
- The dispatcher runs on your own host; it does not provide a hosted service or public API.
- Telegram delivery is best-effort at-least-once with local deduplication, not a mathematically guaranteed exactly-once transaction. An interrupted send may have an ambiguous outcome and is flagged for review; a generic retry excludes it.
- Telegram notifications do not create bots, authenticate Telegram accounts, alter Drive permissions, or replay historical deliveries on first enablement.
- Google Drive folder IDs must remain stable; renaming a project in config does not rename the Drive folder.

## More documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/DISPATCH.md](docs/DISPATCH.md)
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- [docs/AI-QUICKSTART.md](docs/AI-QUICKSTART.md)
- [docs/SECURE-CODING.md](docs/SECURE-CODING.md)
