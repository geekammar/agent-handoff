# Dispatcher (v1.1, v1.2 P0/P1)

v1.2 P0 adds observable progress, actionable `dispatch status`, and safe interruption/restart reporting. v1.2 P1 adds an optional, disabled-by-default Telegram outbox with a verified report link and an Arabic ChatGPT handoff prompt. C1 adds an opt-in secure coding execution foundation: per-project permission policy, a dedicated non-root child identity, an allowlisted child environment, dedicated HOME/XDG/auth, and workspace safety validation. C1 does not enable coding tasks in production and does not implement Draft PR automation. The durable ledger, request contract, upload verification, issue publication, and re-execution rules below are unchanged. See [SECURE-CODING.md](SECURE-CODING.md).

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
- Optional durable Telegram notifications after verified delivery, with bounded retries and a copy-ready Arabic ChatGPT prompt.
- Opt-in C1 isolation foundation for future coding tasks: per-project policy, dedicated Unix identity, allowlisted child environment, dedicated model auth, and workspace validation.

Not implemented:

- AGY dispatch.
- Claude Code, Gemini CLI, Codex CLI, or other execution adapters.
- SAME SESSION continuation. It is rejected unless a safe continuation implementation exists.
- Automatic project inference or model selection.
- A dashboard, database server, public API, SaaS hosting, or multi-user service.
- Telegram bot creation, Telegram account login, managed bot provisioning, or a generic notification platform.
- Coding-task automation, test execution, commit/branch creation, or Draft PR publication (C2).
- OS account creation, provider-key creation, or production workspace provisioning.

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
      "allow_write": false,
      "isolation": {
        "enabled": false,
        "environment": "allowlist",
        "identity": {
          "user": "agent-handoff-dev",
          "group": "agent-handoff-dev",
          "home": "/srv/agent-handoff/home/agent-handoff-dev"
        },
        "workspace_roots": ["/srv/agent-handoff/work"],
        "denied_projects": ["Rabet", "Yakfi"],
        "child_path": "/usr/local/bin:/usr/bin:/bin"
      }
    },
    "delivery": {"retry_seconds": 60, "max_attempts": 0},
    "github": {"command": "gh", "require_private": true}
  },
  "notifications": {
    "telegram": {
      "enabled": false,
      "chat_id": "",
      "token_env": "AGENT_HANDOFF_TELEGRAM_TOKEN",
      "timeout_seconds": 10,
      "retry_seconds": 60,
      "max_attempts": 5,
      "delayed_after_attempts": 3
    }
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
| `projects` | Dispatch project name → `{ "workspace": "<canonical absolute path>" }`. The name must match an existing core project; the workspace must exactly match one of that project's recorded paths. An optional `execution` object overrides the global permission policy for that project. |
| `poll_interval_seconds` | Watch-mode polling interval. |
| `execution.timeout_seconds` | Bounded OpenCode execution timeout. |
| `execution.max_prompt_bytes` | Maximum Issue instruction size. |
| `execution.permission_profile` | `read_only` (default) or `workspace_write`. |
| `execution.allow_write` | Must be explicitly true with `workspace_write`. Must be a JSON boolean. |
| `projects.<name>.execution.permission_profile` | Optional per-project `read_only` or `workspace_write`. Missing fields inherit the global policy. |
| `projects.<name>.execution.allow_write` | A project-level `workspace_write` requires explicit `true` in the same project override. |
| `execution.isolation.enabled` | `false` by default. When true, the child runs as the dedicated non-root identity with an allowlisted environment and validated workspace. |
| `execution.isolation.identity` | `user`, optional primary `group`, and dedicated `home`. No root or privileged group membership. |
| `execution.isolation.workspace_roots` | Safe canonical roots; isolated workspaces must be inside one of them. |
| `execution.isolation.denied_projects` | Additional denied project names. `Rabet` and `Yakfi` are always denied in isolated mode. |
| `execution.isolation.child_path` | Reviewed executable PATH for the child; system directories by default. |
| `delivery.retry_seconds` | Base backoff for GitHub publication retries. |
| `delivery.max_attempts` | `0` means retry indefinitely. A non-zero value stops automatic publication retries after that many failures; the verified upload is preserved and `dispatch deliver` can publish manually. |
| `github.command` | `gh` executable name or absolute path. |
| `github.require_private` | Must remain `true`. |
| `notifications.telegram.enabled` | `false` by default. Must be explicitly true to send anything. |
| `notifications.telegram.chat_id` | Numeric Telegram chat id (negative for groups/channels) or `@channelusername`. |
| `notifications.telegram.token_env` | Environment variable **name** that holds the bot token. Defaults to `AGENT_HANDOFF_TELEGRAM_TOKEN`. The token itself never belongs in JSON. |
| `notifications.telegram.timeout_seconds` | Per-request Telegram HTTP timeout, bounded `1..120`. |
| `notifications.telegram.retry_seconds` | Base notification backoff, bounded `5..3600`. |
| `notifications.telegram.max_attempts` | Bounded send attempts, `1..20`. After exhaustion the notification alone becomes `NEEDS_ATTENTION`. |
| `notifications.telegram.delayed_after_attempts` | Upload/publication failure threshold before one low-noise `DELIVERY_DELAYED` message per task/run. Default `3`. |

`config validate` reports disabled notifications as valid without requiring a token or chat id. Enabled notifications require both and fail with an actionable message that never contains the token value. Disabled notifications never create a Telegram transport and never make an outbound request. Restart a running dispatcher service after changing notification configuration; the service reads it at startup.

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
  notifications/
    notifications.lock
    activated_at
    records/<notification-id>.json
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

`dispatch deliver` reconciles an existing run manually and never invokes an agent. After publication it enqueues and attempts the completion notification, but a Telegram failure does not change the successful delivery result.

## Telegram notifications (v1.2 P1)

The notification outbox is a separate durable workflow:

```text
Verified/attention dispatch state
        |
        v
stable notification identity: project + task + run + kind
        |
        v
PENDING -> SENDING -> SENT
             |               |   v
             | RETRY_WAIT (bounded backoff)
             v
       NEEDS_ATTENTION (attempts exhausted or ambiguous interruption)

PENDING/RETRY_WAIT -> SUPERSEDED (task condition changed before sending; retained for audit)
```

Notification kinds:

| Kind | Trigger and content |
| --- | --- |
| `DELIVERY_COMPLETED` | Only when the ledger is `ISSUE_UPDATED`, `upload.verified=true`, a direct report URL exists, the upload receipt URL is verified, and `publication.status=PUBLISHED`. Contains the exact verified report URL and the Issue URL. |
| `NEEDS_ATTENTION` | Uncertain execution, capture failure requiring review, or exhausted GitHub publication retries. Uses the real status/reason. If no verified report URL exists, it says so explicitly. |
| `DELIVERY_DELAYED` | A captured task with upload still failing, or a verified task with publication still failing. The message distinguishes the two situations and never describes an unverified upload as a verified report. Thresholded by `delayed_after_attempts`. |

A completion message is never sent from OpenCode text, `REPORT_CAPTURED`, a local test result, a readable Drive folder, or an unverified URL. Execution outcome is reported separately from report delivery. A failed or blocked execution still gets a delivery notification if and only if its report was verified and published, and the message states the outcome honestly.

### Message and ChatGPT prompt

A completion message contains a selectable copy-ready Arabic prompt with the exact verified URL. Inline URL buttons are `View Report` and `View GitHub Issue`. There is **no** native Telegram copy button. The prompt says to read the report and review the code/tests; it does not claim that ChatGPT has already read it.

```text
✅ تم تسليم تقرير المهمة

Project: ProjectA
Task: Fix login redirect
Status: Verified & Delivered
Execution outcome: succeeded

Report:
https://drive.google.com/file/d/<verified-file-id>/view

PROMPT FOR CHATGPT:

تم الانتهاء من مهمة ProjectA — Fix login redirect.
اقرأ تقرير التنفيذ:
https://drive.google.com/file/d/<verified-file-id>/view
وراجع الكود والاختبارات، ثم أخبرني بالنتيجة
والإصلاحات والمتبقي والخطوة التالية.
```

Dynamic text is HTML-escaped. Titles and reasons are shortened before the verified report URL is ever touched. The bot does not change Drive sharing; the Telegram recipient must already have access to a private Drive file. Messages never include Issue instructions, prompts, transcripts, local paths, credentials, or report content.

### Idempotency, retry, and interruption

- Repeated polls do not resend a `SENT` completion.
- The outbox is independent of the ledger: a Telegram failure cannot change `ISSUE_UPDATED`, rerun OpenCode, re-upload a report, or repost the Issue comment.
- Pending notifications survive dispatcher restart and are retried by later polls.
- Before a queued notification is sent, its related durable dispatch record is resolved and the current condition is checked. If the upload recovered, publication completed, or an attention condition was resolved, the obsolete notification becomes `SUPERSEDED` with a persisted reason and timestamp. It is never sent and never deleted.
- When notifications are first enabled, the service writes an activation marker before processing transitions. Reconciliation uses the relevant lifecycle transition timestamp, not generic `updated_at`: `publication.published_at` for completion, the recorded attention transition for `NEEDS_ATTENTION`, and recorded upload/publication failure or exhaustion timestamps for delayed/attention recovery. A crash between a transition and the outbox write is recovered only when that real transition is at or after activation. Historical records without a trustworthy timestamp fail closed.
- A send is persisted as `SENDING` before the HTTP call. If the process disappears during the call, a later drain sees the stale `SENDING` record and marks it `NEEDS_ATTENTION` with `ambiguous=true` instead of silently resending; the outcome is inherently ambiguous.
- Generic `dispatch notify --retry` resets and retries only confirmed retryable failures. It never requeues an ambiguous send. After reviewing the Telegram chat, an operator can retry one specific notification with `--notification <id> --acknowledge-duplicate`; the acknowledgement and its history are persisted. A failed acknowledged retry returns to `NEEDS_ATTENTION` and is not retried automatically.
- Telegram network sends run after the dispatcher runner lock is released; the outbox has its own lock, so a slow Telegram endpoint does not block a new dispatcher poll.
- Telegram `429` with `parameters.retry_after` is honored up to a bounded delay.
- `dispatch run --force` retries upload/publication work; notification retry remains on its own backoff. Use `dispatch notify --retry` for Telegram.

### Activation boundary

Notifications are enqueued at lifecycle transitions observed while `notifications.telegram.enabled` is true, and recovered from ledger records whose real transition timestamp is at or after the durable `activated_at` boundary. Historical `ISSUE_UPDATED` records older than the marker are never replayed, even if a later metadata update changes their generic `updated_at`, so enabling Telegram does not flood the owner with every old delivery. Records that genuinely complete or enter attention after enablement can still be recovered.

`dispatch deliver` reconciles an existing run manually and never invokes an agent.

An interrupted command (for example Ctrl-C during a slow upload) leaves the durable record `UPLOAD_PENDING` with capture already complete. A later `dispatch run` retries the upload with the same task/run identity and session. It does not start another OpenCode session, and an existing delivery marker prevents a duplicate issue comment.

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

`upload.attempts` in the dispatch ledger mirrors the uploader's counter in `COMPLETE.json`. It counts total upload attempts that reached a success or failure result, including the successful attempt: a first-attempt success is `1`, one failure followed by success is `2`, and two failures followed by success is `3`. Receipt or `COMPLETE.json` reconciliation, reuse of an already verified upload, publication retries, and status reads never increment it. Ledger updates copy the authoritative count and do not lower a previously recorded value. Legacy records without the field are read as `0` and are corrected when a verified `COMPLETE.json` provides the count.

## Commands

```bash
# One poll, then exit
agent-handoff dispatch run --once --json

# Continuous polling (foreground; use a service manager for persistence)
agent-handoff dispatch run --watch

# Human progress on stderr even when stderr is not a TTY
agent-handoff dispatch run --once --progress

# Suppress human progress/summary; JSON stdout is unchanged
agent-handoff dispatch run --once --json --progress --quiet

# Inspect durable records with health, links, retries, and next actions
agent-handoff dispatch status
agent-handoff dispatch status --json

# Read local + remote metadata for one run without changes
agent-handoff dispatch inspect --task <task-id> --run <run-id> --json

# Reconcile and publish an existing run; never runs an agent
agent-handoff dispatch deliver --task <task-id> --run <run-id> \
  --issue <number> --repository owner/private-dispatch --json

# Inspect the notification outbox without sending anything
agent-handoff dispatch notify --json
agent-handoff dispatch notify --all --json

# Retry confirmed notification failures only; no agent, upload, or Issue comment is repeated
agent-handoff dispatch notify --retry --json
agent-handoff dispatch notify --retry --task <task-id> --run <run-id> --json

# After reviewing Telegram, retry one ambiguous send with explicit duplicate-risk acknowledgement
agent-handoff dispatch notify --retry --notification <notification-id> --acknowledge-duplicate --json
```

`dispatch run` refuses to start when `dispatch.enabled` is false. `config validate` validates core and dispatch configuration without contacting GitHub or Drive.

## Progress and machine output (v1.2 P0)

Ordinary interactive use writes short, elapsed-time progress lines to **stderr**. `--json` stdout remains a single valid JSON object even when progress is enabled. Progress is opt-in for non-interactive stderr via `--progress`; `--quiet` suppresses human progress and summaries. `dispatch deliver` supports the same two flags.

Progress stages describe observed work, not guesses:

```text
discovering -> discovered -> validated -> claimed -> executing -> executed
-> capturing -> captured -> uploading -> waiting -> verifying -> receipt
-> reconciling -> publishing -> completed -> notifying -> notified
```

- Long operations say they are bounded and cite the configured timeout; no total ETA is invented.
- Long OpenCode execution emits a periodic `executing` heartbeat with elapsed seconds, the number of parsed events, and the configured execution timeout. It never includes prompt text or model output.
- A single slow rclone upload, folder listing, MD5/read-back verification, or Drive file-id lookup emits a periodic `waiting` heartbeat with elapsed seconds and the bounded timeout. It never estimates remaining time.
- GitHub marker lookup and comment posting are named progress stages; `gh` calls remain bounded by their own timeout.
- `notifying`/`notified` stages describe the independent Telegram outbox; they never imply that a failed notification changed report delivery.
- Failures add a `retryable` stage with the next retry time and state whether only the upload or only the publication is retried. When `delivery.max_attempts` is reached, publication becomes `EXHAUSTED` and `dispatch status` asks for a manual `dispatch deliver`.
- Upload retries honor the recorded `next_attempt_at`; a normal poll does not bypass backoff. `--force` performs an operator-controlled immediate retry.
- Progress never includes Issue instructions, prompt text, environment contents, tokens, or local workspace paths. Local paths in error text are replaced with `<local>` before printing.
- The final poll summary includes `elapsed_seconds`. In watch mode, elapsed time resets for each poll.

In `--watch --json` mode, each poll writes one compact JSON object per line (newline-delimited JSON); `--once --json` writes one pretty-printed object. Progress remains on stderr.

`dispatch status` groups records as executing/current, stalled, needs attention, retryable, pending, or delivered. Each entry shows the Issue link, a verified report link when one exists, the delivery comment, notification state, the next retry when known, and one concrete next action. The JSON form preserves the previous record fields and adds `health`, `phase`, `stalled`, `age_seconds`, `elapsed_seconds`, `next_action`, `next_attempt_at`, `last_error`, per-record `notifications`, and a top-level notification summary with counts.

A delivered task can have a pending Telegram notification. That is not a task failure and does not change `health=delivered`; the status output separates the two facts.

Stalled detection uses the recorded runner PID when present. A `RUNNING` or `CLAIMED` record whose dispatcher process is gone is reported as stalled; the next `dispatch run` reconciles persisted session output or marks the request `NEEDS_ATTENTION` without re-executing OpenCode.

### Poll summary counters

`needs_attention` in the final poll summary counts records that transitioned into an attention state during that poll. This includes execution or capture conditions that require review and a publication that first becomes `EXHAUSTED` because `dispatch.delivery.max_attempts` was reached. It does not recount records that were already in attention at the start of the poll, and a forced retry of an already exhausted publication does not increment it again. `rejected` remains a separate counter. `dispatch status` is authoritative for the current set of attention records.

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

## Security limitations

- `read_only` is an OpenCode tool-permission policy: the `plan` agent is used and `edit`/`bash` and other unspecified tools are denied through `OPENCODE_CONFIG_CONTENT`. It is **not** an operating-system sandbox. The dispatcher process runs with the Unix permissions of the user who started it.
- A user who can modify the runtime config, the OpenCode binary, the model/provider, or the host can change behavior.
- C1 isolation drops the OpenCode child to a dedicated non-root UID/GID with empty supplementary groups and an allowlisted environment. The parent dispatcher still runs as root with its credentials; this is UID/environment separation, not full OS sandboxing. CPU/memory/disk limits, network policy, prompt-injection resistance, and hostile test code are not fully solved by C1. See [SECURE-CODING.md](SECURE-CODING.md).
- SIGINT and SIGTERM attempt to stop the OpenCode and rclone child process groups before the dispatcher exits. A forced `SIGKILL` cannot run cleanup, so an already-started OpenCode child may continue. The ledger prevents automatic re-execution and marks the request `NEEDS_ATTENTION`; read-only mode limits the effect, but `workspace_write` requires operator care.
- `allowed_users` matches GitHub login names, not immutable account IDs. Login renames must be handled by the repository owner.
- Isolated coding is opt-in and disabled by default. Production Rabet/Yakfi remain read-only. A clean Git tree, canonical path, and validated ownership do not prove that repository content or tests are safe; C2 must use disposable worktrees and review publication.
- Uploaded `REPORT.md` files currently include the local run directory path as audit evidence. They are stored in the user's private Drive; GitHub delivery comments contain only identifiers and verified URLs. Upload receipts omit local filesystem paths.
- Telegram credentials are read from the configured environment variable only. The bot token is never written to config, reports, notification records, logs, or exception text. The outbox stores report/Issue URLs and sanitized text, not Issue instructions or local paths. The transport always targets the official `https://api.telegram.org` endpoint; alternate test endpoints require explicit injection by test code and cannot be selected from runtime configuration.
- The dispatcher removes the default and configured notification token environment variables from the OpenCode child environment for both `read_only` and `workspace_write` profiles. This is defense in depth, not an OS sandbox: a child running under the same Unix account may still access credential files or unrelated parent secrets, and model-authentication variables remain available because OpenCode legitimately needs them.
- Telegram's Bot API has no universal end-to-end exactly-once transaction with the local filesystem. The outbox provides local deduplication, at-least-once retries, stale-intent suppression, and explicit ambiguous-send review; it does not claim mathematically guaranteed exactly-once delivery.
- The model can read files allowed by the OpenCode read permission within the workspace. `.env`-style files are denied by pattern, but this is not a substitute for OS-level secret isolation.

## Failure recovery

```bash
agent-handoff dispatch status --json
```

- `UPLOAD_PENDING`: Drive unavailable or verification failed. Retry upload only. `dispatch status` shows the next retry and the exact action.
- `UPLOAD_VERIFIED`: publication pending/failed. Retry comment only. The verified upload is reused.
- `NEEDS_ATTENTION`: an execution was uncertain and no recoverable output exists. Inspect the host and session evidence; do not blindly rerun. Submit a new issue if the work must be repeated.
- `REJECTED`: malformed or unauthorized request. A new, corrected issue is required; editing an old rejected issue does not automatically execute it.

Notification-only states are reported separately from the task record:

- `RETRY_WAIT`: Telegram or the configured recipient failed temporarily. The task remains delivered. Retry is automatic after its own backoff, or use `dispatch notify --retry` for confirmed failures.
- `NEEDS_ATTENTION` (notification): notification attempts were exhausted, the related dispatch record is unavailable, or an interrupted send has an ambiguous outcome. For a confirmed cause, fix it and use `dispatch notify --retry`. For an ambiguous send, review Telegram first and use a targeted `--notification <id> --acknowledge-duplicate` only if a duplicate is acceptable. The report delivery is still intact.
- `SUPERSEDED`: the task recovered or the attention condition was resolved before the message was sent. Nothing is sent, and the record is retained for audit.
- `PENDING`/`SENDING`: queued or in progress. A pending notification survives restart.

Backoff is automatic. `--force` ignores upload/publication backoff for an operator-controlled retry; Telegram retries use `dispatch notify --retry`.

## Tests

Deterministic tests use fake GitHub, fake OpenCode, and fake rclone. They cover:

- valid request executes once;
- duplicate polling does not repeat execution;
- unauthorized/malformed/unknown project/unknown model/SAME SESSION rejection;
- project and session isolation;
- cwd/PWD correctness;
- execution → task/run attribution;
- upload retry without model restart and upload backoff without `--force`;
- upload attempt counts including the successful first attempt, multiple failures, restart, verified reuse, and legacy records without the field;
- publication retry without duplicate links and `delivery.max_attempts` exhaustion;
- publication exhaustion counted once in the poll summary and recovered by manual delivery without re-execution;
- long OpenCode execution heartbeat with no prompt or path leakage;
- execution timeout and dispatcher SIGTERM terminating the child process;
- restart after claim and after verified upload;
- stale remote `COMPLETE.json` reconciliation and receipts;
- `--watch --json` newline-delimited JSON purity;
- core capture with dispatch disabled;
- notification completion only after `ISSUE_UPDATED` + verified upload/receipt + confirmed publication;
- no completion notification for upload failure or failed/unexhausted publication;
- one low-noise delayed notification and one truthful attention notification per task/run;
- repeated polling, restart recovery, stale/ambiguous sending, invalid chat id, token redaction, rate-limit `retry_after`, Arabic/HTML/length handling, and disabled-by-default no-network behavior;
- manual delivery notification without agent re-execution;
- activation boundary that does not replay historical delivered records, even after a generic metadata update;
- stale delayed/attention notification suppression with durable `SUPERSEDED` state;
- ambiguous-send exclusion from generic retry and targeted acknowledged retry;
- truthful upload-failure and publication-failure delayed messages;
- fixed official Telegram endpoint with explicit fake-transport injection in tests;
- configuration preservation across install/upgrade/rollback;
- per-project policy inheritance and explicit write authorization;
- legacy Rabet/Yakfi-style read-only inheritance;
- issue metadata cannot change policy, identity, workspace, or model allowlist;
- dedicated identity fail-closed behavior, privileged-group rejection, and env allowlist;
- canonical/symlink/protected/dirty workspace refusal without resetting work;
- timeout and orphan-process cleanup with progress unchanged.

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Live acceptance requires real `gh`, OpenCode, and Drive credentials. If any are unavailable, report the limitation; do not invent a pass.
