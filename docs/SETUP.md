# Guided setup foundation

This document is the canonical contract for the P1-B1 guided setup foundation. It covers human users and AI coding agents that may or may not have shell access. Setup planning is read-only; configuration changes are narrowly scoped, validated, backed up, and require explicit approval.

The existing commands remain the source of truth for each operation: `agent-handoff auth`, `agent-handoff project add`, `agent-handoff doctor`, `agent-handoff config validate`, and the optional dispatcher commands.

## Entry points

```bash
agent-handoff setup plan
agent-handoff setup plan --json
```

`setup plan` never writes configuration, credentials, service units, or reports. It runs local discovery, reports completed and remaining steps, and previews any requested configuration change.

```bash
agent-handoff setup apply --approve --drive-root-folder-id <ROOT_FOLDER_ID> --json
agent-handoff setup recover --approve --json
```

`setup apply` can only change the explicitly supported setup fields. It cannot enable the dispatcher, coding, `workspace_write`, isolation, notifications, or a service. `setup recover` finalizes a safe cleanup after an interrupted apply.

## Setup phases

The foundation keeps five concerns separate:

| Phase | Command or contract |
| --- | --- |
| A. Environment discovery | `agent-handoff setup plan --json` |
| B. Prerequisite validation | `setup plan` checks, `agent-handoff config validate` |
| C. Configuration planning | `setup plan` with supported patch flags |
| D. Safe configuration application | `setup apply --approve` |
| E. Post-setup verification | `agent-handoff doctor`, one upload-verified capture |

Planning does not perform phase D unless the user runs `setup apply` with `--approve`.

## Capability honesty for AI assistants

| You have | You can |
| --- | --- |
| Repository text only | Explain the commands and ask the user to run them. |
| Shell on the target host | Run discovery, install from a trusted checkout, plan, apply approved configuration, and inspect verification output. |
| Shell + `gh` authorization | Verify the optional private Issue inbox. |
| Shell + rclone Drive authorization | Upload and verify reports. |
| No shell or no credentials | Ask the user to run each command and return only non-secret output. Never claim installation, authorization, upload, or verification succeeded. |

An AI session running in a browser or chat does not automatically have access to a user's VPS. It must not claim it ran commands it did not run.

Never ask for or print bot tokens, rclone configuration contents, `.env` contents, OAuth JSON, or SSH private keys.

## Discovery checks

`setup plan --json` returns a stable schema. Every check contains:

```json
{
  "id": "rclone",
  "scope": "core",
  "status": "missing",
  "summary": "rclone is missing",
  "detail": null,
  "next_action": "Install rclone. It is required for upload-verified reports.",
  "blocking": false
}
```

Statuses:

| Status | Meaning |
| --- | --- |
| `ready` | The check is satisfied. |
| `missing` | A prerequisite in that scope is not present or not configured; it is an actionable setup step. |
| `optional` | An optional integration is absent or deliberately disabled. |
| `blocked` | The host or configuration is unusable until an issue is resolved. |

Scopes separate required core capture from optional integrations:

| Scope | Examples |
| --- | --- |
| `core` | Linux, Python 3.10+, rclone, Agent Handoff installation, runtime configuration, Drive authorization, project mapping, recovery state |
| `install` | git for source checkout, install, upgrade, and rollback |
| `capture` | OpenCode for automatic capture |
| `dispatch` | GitHub CLI, GitHub authorization, dispatcher configuration and service unit |
| `coding` | separately owned coding policy; always reported as optional here |

The plan's `ok` field covers core and install blockers. A dispatch-only blocker sets `all_scopes_ready: false` without making core capture unusable.

Credential values are never read into plan output. The plan reports booleans and explanations only. `gh auth status` output is discarded.

## Plan output

A JSON plan includes:

- `schema_version`, `command`, `status`, `ok`, `core_ready`, and `all_scopes_ready`;
- `platform`, `versions`, `installation`, `state`, and `checks`;
- `steps` plus `completed_steps`, `remaining_required_steps`, `blocked_required_steps`, and `optional_steps`;
- `next_actions` with safe commands, approval flags, and placeholder markers;
- `blockers` with scope and next action;
- `recovery` state for an interrupted apply;
- `config_plan` with requested fields, changes, validation errors, backup and atomic-write expectations, and forbidden activations;
- `approval_required`.

The plan does not include timestamps, random identifiers, credential values, repository credentials, or service tokens. Repeating the same plan on the same host and configuration produces the same content.

## Supported configuration fields

`setup apply` deliberately supports a small allowlist:

| Flag | Config path |
| --- | --- |
| `--drive-parent-folder-id` | `drive.parent_folder_id` |
| `--drive-parent-folder-name` | `drive.parent_folder_name` |
| `--drive-root-folder-id` | `drive.root_folder_id` |
| `--drive-root-folder-name` | `drive.root_folder_name` |
| `--workspace-root` | `workspace_roots` (merged; existing roots preserved) |
| `--auto-register-projects` / `--no-auto-register-projects` | `auto_register_projects` |

Everything else is excluded. In particular, `setup apply` rejects:

- `dispatch`, `dispatch.coding`, `dispatch.execution.isolation`, `workspace_write`, and `allow_write`;
- `notifications` and any token-like field;
- project mappings, runtime storage paths, security settings, upload settings, integrations, and unknown fields.

Project mappings are added with `agent-handoff project add`, which verifies the Drive folder. Dispatcher and coding policy remain separate owner decisions in `docs/DISPATCH.md` and `docs/SECURE-CODING.md`.

## Apply safety contract

`setup apply --approve`:

1. refuses to overwrite an invalid JSON file or a symbolic-link configuration;
2. preserves unknown existing fields and every unrelated project, dispatch, notification, and security value;
3. validates the proposed runtime view with the existing core, dispatcher, and notification validators before writing;
4. writes a mode-600 backup when a configuration file already exists;
5. writes a recovery journal, then replaces the configuration atomically with mode 600;
6. re-reads and re-validates the written file;
7. restores the previous file if validation, a write step, or the round-trip check fails;
8. leaves a retained backup for manual recovery;
9. is idempotent: repeated execution with unchanged values reports `changed: false` and writes nothing;
10. never activates a dispatcher service, coding, isolation, or notifications.

Approval is required whenever configuration content would change. Running `setup apply --json` without `--approve` prints a valid JSON preview and exits non-zero with `approval_required: true`.

No apply operation stores a secret in configuration. Drive and GitHub authorization stay in rclone and `gh`; the Telegram token stays only in the environment variable named by `notifications.telegram.token_env`.

## Interrupted apply and recovery

Before replacing the configuration, `setup apply` writes a mode-600 journal that records the previous and proposed SHA-256 values and the backup path.

If the process is interrupted:

- `setup plan` reports `setup.recovery` as blocked and names the recovery status;
- `setup recover --approve` finalizes cleanup when the current file exactly matches either recorded state;
- an ambiguous state is never guessed or overwritten; the journal and backup remain for manual review.

Do not delete a setup journal by hand without comparing the configuration and backup first.

## Recommended setup flow

After installing from a trusted checkout:

```bash
agent-handoff setup plan --json
```

Then, using values that belong to the user:

```bash
# Optional: create the parent and reports folders in the user's own Drive first,
# then provide their ids. Never reuse another user's ids.
agent-handoff setup apply --approve --json \
  --drive-parent-folder-id <PARENT_FOLDER_ID> \
  --drive-root-folder-id <ROOT_FOLDER_ID>

agent-handoff auth
agent-handoff auth --check
agent-handoff project add --name ProjectA --path ~/projects/project-a
agent-handoff config validate --json
agent-handoff doctor --json
```

A setup is not verified until at least one run has `upload.verified: true` with a direct Drive URL and a verified upload receipt. A successful command or an existing folder is not upload verification.

## GitHub authorization

```bash
gh auth status
gh auth login
```

Use the user's own account. The plan only reports whether `gh auth status` succeeds; it never prints its output. Do not paste tokens into chat, reports, Issues, or configuration. The optional dispatcher repository must be private with Issues enabled.

## Google Drive authorization

Interactive browser flow:

```bash
agent-handoff auth
agent-handoff auth --check
```

Headless flow:

1. On a browser-capable machine run `rclone authorize "drive"`.
2. Sign in with the account that owns the Drive reports folder.
3. Save the token JSON as a mode-600 file on the target host.
4. Import it with `agent-handoff auth --import-token-file /path/to/token.json`.
5. Remove the token file after verifying.

Never paste the token JSON into a chat or a report. Drive authorization is outside runtime configuration and is never included in plans.

## Selecting an OpenCode model

Guided setup does not select or invent models. For optional dispatch:

```bash
opencode models
```

Use an exact provider/model ID from that output in `dispatch.models`. Issue text may select only a configured alias. Guided setup never adds a model alias or enables dispatch.

## Verifying the finished setup

```bash
agent-handoff setup plan --json
agent-handoff config validate --json
agent-handoff doctor --json
agent-handoff list --project ProjectA --json
```

Confirm the four facts separately:

1. installation and configuration are present and valid;
2. Drive authorization and project hierarchy are verified;
3. at least one run is upload-verified;
4. any dispatcher, publication, or notification claim has its own evidence.

A task/agent outcome does not prove tests or deployment. A Drive folder existing is not upload verification. A GitHub comment existing is not upload verification.

## Continuing without shell access

An AI without shell access must provide the documented commands and ask for only non-secret summaries. It must not say "installed", "authorized", "uploaded", or "verified" unless a tool result actually shows that outcome.

Suggested wording:

> I can guide the setup, but this chat cannot run commands on your VPS, authorize your GitHub account, or read your Google Drive. Run `agent-handoff setup plan --json` and share only the non-secret summary. I will not claim a step succeeded until the command output shows it.

## Explicit non-goals

P1-B1 does not provide:

- a full interactive wizard or a second configuration system;
- automatic credential acquisition, OAuth login, bot creation, or project inference;
- automatic model selection;
- dispatcher, coding, `workspace_write`, isolation, or service activation;
- a web server, dashboard, database, or public API;
- any production deployment or PR merge.
