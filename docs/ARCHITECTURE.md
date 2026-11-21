# Architecture

## Goals

Agent Handoff records what an agent did, separately from whether tests or deployment succeeded, and verifies that each report reached Google Drive. The v1.1 addition lets a private GitHub Issue request an OpenCode task in one explicitly configured local project, without coupling execution state to upload or publication state.

The design favors a small standard-library Python core, atomic filesystem operations, explicit integration boundaries, and operator-owned credentials.

## Runtime separation

Source code and runtime data are separate:

- Source checkout: any Git working tree, for example `~/src/agent-handoff`.
- Runtime home: `$AGENT_HANDOFF_HOME`, for example `~/agent-handoff`.
- Runtime state: `data/`, `queue/`, `state/`, `locks/`, `logs/`, `tmp/`, and `config/config.json`.
- External credentials: rclone configuration and OAuth token, normally under `~/.config/rclone/`; GitHub auth is owned by `gh`.

The installer applies one source-selection policy to Git and non-Git checkouts. Only regular files under managed source directories are copied; runtime `config/config.json`, credentials, runtime state, generated reports, live-test configuration, backups, and test artifacts are excluded even if present in a Git index or archive. Source symlinks are skipped and destination symlink components are rejected. `setup/managed.py` owns this policy and the installed-file manifest.

## Components

### Core CLI (`lib/agent_handoff`)

- `cli.py`: argument parsing and command handlers.
- `config.py`: defaults, runtime config, project name/path resolution, auto-registration.
- `report.py`: required-section enforcement and report rendering.
- `store.py`: task/run directories, atomic JSON/Markdown writes, session map, indexes, queue items.
- `upload.py`: rclone command wrapper, hierarchy checks, upload, MD5/read-back verification, retry backoff, worker lock, direct Drive file ID lookup, remote metadata reads.
- `util.py`: locks, atomic writes, IDs, redaction, command execution.
- `adapters/common.py`: shared automatic-session ingest pipeline.
- `adapters/agy.py`: AGY transcript parser and `Stop` hook adapter.

### Optional dispatcher (`lib/agent_handoff/dispatch`)

- `config.py`: validates and normalizes trusted dispatcher configuration; rejects cross-project workspaces and writable profiles without explicit consent.
- `requests.py`: versioned Issue metadata parser and trusted alias resolution.
- `ledger.py`: atomic durable records for execution, capture, upload, and publication; one record per repository + issue number.
- `github.py`: `gh`-based transport for repository verification, issue polling, comments, and idempotency-marker lookup.
- `runner.py`: claim/execute/capture/upload/publish state machine, restart recovery, and per-record error isolation.
- `delivery.py`: verified upload reconciliation, direct Drive URL resolution, `UPLOAD-RECEIPT.json`, remote `COMPLETE.json` re-upload, comment rendering, and publication backoff.
- `security.py`: C1 identity, allowlisted child environment, dedicated model-auth, and canonical workspace/protected-path/dirty-tree validation. It never creates an OS account or workspace.
- `coding/`: optional C2 coding workflow. `config.py` parses trusted coding policy; `workspace.py` prepares a disposable clean clone at a verified base commit; `testrunner.py` runs owner-configured argv tests under the isolated identity; `changes.py` validates paths/diff/secrets; `publication.py` builds a parent-owned commit and publishes one branch and one Draft PR; `pipeline.py` implements durable stages and recovery; `report.py` renders coding evidence into the existing report contract.
- `adapters/base.py`: small execution-adapter protocol so a future CLI adapter can be added without changing the ledger, GitHub transport, project routing, upload, or delivery.
- `adapters/opencode.py`: the only v1.1 execution adapter. Sets both `cwd` and `PWD` to the canonical workspace, uses `--pure`, applies a read-only or explicitly enabled write permission policy, streams JSON events to a mode-600 session file, preserves the session ID as soon as it appears, emits bounded execution heartbeats without prompt content, and terminates its process group on timeout or orderly shutdown. When C1 isolation is enabled for the effective project policy, it launches after identity, model-auth, workspace, ancestor, Git-metadata, and trusted-executable preflight with the configured non-root UID/GID, empty supplementary groups, and an allowlisted environment. Legacy read-only projects can remain non-isolated in the same dispatcher; write-capable projects must resolve to isolation.

### Optional notifications (`lib/agent_handoff/notify`)

- `config.py`: validates the disabled-by-default Telegram section and reads the bot token from a named environment variable without ever returning it in validation errors.
- `store.py`: durable notification outbox, stable notification identity, atomic JSON state, `flock`, bounded retry state, ambiguous-send detection, and terminal `SUPERSEDED` audit records.
- `transport.py`: standard-library Telegram Bot API `sendMessage` client fixed to the official HTTPS endpoint, with bounded timeout, HTTP/`ok=false` handling, `retry_after`, and token redaction. Test endpoints require explicit constructor injection.
- `messages.py`: HTML-escaped Arabic-aware completion, attention, and truthful delayed messages. An unverified upload is never described as a verified report; the completion prompt always contains the exact verified report URL.
- `service.py`: enqueue/drain/retry orchestration with current-condition revalidation before sending. Notification failure is isolated from the dispatcher ledger and never changes execution, verification, upload, or publication state.

### OpenCode capture integration

The active capture integration is the installed OpenCode plugin `~/.config/opencode/plugin/agent-handoff.js`; source copy lives in `integrations/opencode/agent-handoff.js`.

The plugin uses the OpenCode 1.19.5 plugin event surface:

- `chat.message` captures prompt text and model metadata.
- `session.idle` finalizes the session.
- `client.session.get` and `client.session.messages` retrieve session metadata and messages.
- Subagent sessions are skipped by checking `parentID`.

The plugin writes a mode-600 JSON payload under the runtime `tmp/` directory and invokes `agent-handoff auto-complete --payload-file ...`. The payload is deleted afterward. The plugin never uploads payloads or terminal transcripts.

Dispatcher executions use `--pure` and are captured by the dispatcher itself, so one dispatched session does not create a second plugin-generated report.

### AGY capture integration

AGY 1.5.9 exposes a plugin command surface, but the verified integration is the global lifecycle hook:

- Hook configuration: `~/.gemini/config/hooks.json`
- Event: `Stop`
- Command: `agent-handoff hook agy`
- Instructions: `~/.gemini/config/AGENTS.md`

The hook always exits successfully and prints `{}`. The adapter parses the transcript for exact user inputs and completed planner responses, derives an outcome, and calls the same ingest pipeline as OpenCode. AGY dispatch is not implemented in v1.1.

### Storage and queue

Tasks and runs are timestamped with cryptographic random suffixes. Runs never share a directory. A global `flock` serializes task creation, session maps, index generation, and queue mutation. A non-blocking worker lock prevents concurrent upload processes.

Queue items are separate atomic JSON files. A failed upload never deletes the report. A run is marked upload-verified only after every uploaded file passes MD5 or read-back verification. The remote `PROJECT-STATUS.md` is queued only after a run is verified and lists verified runs only.

## Dispatcher data flow

```text
private GitHub Issue
        |
        v
gh transport: repository private + author allowed + metadata parsed
        |
        v
durable ledger: NEW -> CLAIMED -> RUNNING
        |
        v
OpenCode adapter (effective project policy; optional C1 UID/GID drop and allowlisted env;
                  cwd == PWD == validated workspace; exact configured model)
        |
        v
raw JSON event file + session ID persisted incrementally
        |
        v
Agent Handoff core: task/run + REPORT.md + COMPLETE.json + queue
        |
        v
rclone upload + MD5/read-back verification
        |
        v
Drive file ID -> direct URL -> UPLOAD-RECEIPT.json -> verified COMPLETE.json
        |
        v
idempotent GitHub comment: marker + direct URL -> ISSUE_UPDATED
        |
        +--> optional notification outbox (independent failure domain)
                    |
                    v
             Telegram Bot API sendMessage -> SENT / RETRY_WAIT / NEEDS_ATTENTION
```

Report delivery and notification delivery are separate commit points. The dispatcher records `ISSUE_UPDATED` before a notification exists. A Telegram failure cannot roll that state back, rerun OpenCode, re-upload a verified report, or post a second Issue comment.

## Dispatcher state model

Execution, capture, upload, and publication are separate facts:

```text
execution.status:  NEW | CLAIMED | RUNNING | COMPLETED | FAILED | UNCERTAIN
capture.status:    PENDING | CAPTURED | FAILED | NOT_STARTED
upload.status:     PENDING | VERIFIED | FAILED | NOT_STARTED
publication.status:PENDING | PUBLISHED | FAILED | EXHAUSTED | NOT_STARTED
```

The top-level lifecycle is a convenience view:

```text
NEW -> CLAIMED -> RUNNING -> EXECUTION_COMPLETED
    -> REPORT_CAPTURED / UPLOAD_PENDING -> UPLOAD_VERIFIED -> ISSUE_UPDATED
```

Coding records add `coding.stage`: `REQUEST_ACCEPTED`, `WORKSPACE_PREPARED`, `AGENT_RUNNING`, `AGENT_COMPLETED`, `TESTING`, `VALIDATING_DIFF`, `COMMIT_PREPARED`, `BRANCH_PUBLISHED`, `PR_CREATED`, plus the existing report/upload states. Milestones distinguish agent completion, tests passed, Draft PR publication, and report delivery.

`REJECTED` and `NEEDS_ATTENTION` are terminal/exceptional outcomes. A record that reached `CLAIMED` or later is never automatically executed again. Coding stages can resume tests/publication and idempotent Git/GitHub operations, but never rerun OpenCode without persisted output.

## Notification outbox

Notification identity is project + task + run + kind. Each identity has one durable JSON record under `state/dispatch/notifications/records/`; a `flock` protects mutations. States are `PENDING`, `SENDING`, `SENT`, `RETRY_WAIT`, `NEEDS_ATTENTION`, and `SUPERSEDED`.

Before sending, the service resolves the related dispatch record and rechecks the current condition. A delayed message whose upload or publication recovered, or an attention message whose condition was resolved, becomes `SUPERSEDED` with a reason and timestamp. A missing or unreadable dispatch record is preserved as unresolved attention rather than guessed valid. `SUPERSEDED` and `SENT` records are never retried or deleted.

`DELIVERY_COMPLETED` is enqueued only at an `ISSUE_UPDATED` transition that also has `upload.verified`, a direct report URL, a verified receipt URL, and `publication.status=PUBLISHED`. `NEEDS_ATTENTION` is enqueued at durable attention/exhaustion transitions. `DELIVERY_DELAYED` is enqueued once per task/run after a documented attempt threshold.

The outbox is drained after each dispatcher poll, after the runner lock is released, so a slow Telegram endpoint does not block a new poll. Pending records survive restart. A durable `activated_at` marker bounds reconciliation: each notification kind uses its real lifecycle transition timestamp (`publication.published_at`, recorded attention transition, upload/publication failure, or exhaustion), not generic `updated_at`; older historical records are never backfilled, and a record without a trustworthy timestamp fails closed. Sends use a bounded timeout and bounded attempts with backoff; Telegram `retry_after` is honored when present. A `SENDING` record whose process disappeared is not silently resent: it is marked `NEEDS_ATTENTION` with an explicit ambiguous-outcome warning. Generic retry excludes those ambiguous records; a targeted retry requires a notification id and explicit duplicate-risk acknowledgement.

## Upload, delivery, and historical reconciliation

The core uploader uploads report files and `COMPLETE.json` and verifies them. Because the first `COMPLETE.json` snapshot predates local verification, it can contain `upload.status=pending` even after the report is readable on Drive. This was observed in the historical pilot (issue #4).

v1.1 resolves that truthfully:

1. Treat local verification as the authoritative upload fact.
2. Resolve the actual Drive file ID and direct URL.
3. Write and upload `UPLOAD-RECEIPT.json` with the verified identities and hashes.
4. Re-upload `COMPLETE.json` after adding the verified URL, receipt identity, and verification metadata.
5. Record publication only after the direct URL is available.

`dispatch deliver` applies the same process to an existing run without invoking an agent. It targets only the requested task/run; it does not reconcile unrelated pending queue entries.

## Upgrade model

`setup/upgrade.py` snapshots the installed code to `$AGENT_HANDOFF_HOME/releases/<timestamp>-pre-upgrade/`, runs `setup/install.py`, and restores the snapshot automatically if installation fails. Runtime state and configuration are never part of the snapshot.

The installer records every managed file in `$AGENT_HANDOFF_HOME/.agent-handoff-manifest.json`. Snapshots include `manifest.json` with the files captured for that release. Rollback validates both manifests and every destination path, removes files that are owned by the current installation but absent from the selected release, restores the selected files, and writes the restored ownership manifest. Unknown or unmanaged files, runtime data, runtime configuration, and the dispatch ledger are preserved. Snapshots created before ownership manifests are refused rather than overlaid into a mixed release.

Managed OpenCode and AGY files are captured in memory before installation and restored if installation fails. Invalid external JSON is rejected during preflight, before any plugin, configuration, hook, or instruction file is changed. A successful rollback re-runs the restored release's installer to reconcile managed integration entries while preserving unrelated settings.
