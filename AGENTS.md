# AGENTS.md — Agent Handoff orientation

This is the neutral first entrypoint for coding agents working on or with Agent Handoff. Read this file, then `README.md`, `docs/ARCHITECTURE.md`, and `docs/DISPATCH.md`. Those documents are canonical; tool-specific files are thin pointers.

## What this repository provides

- `agent-handoff`: a small Python CLI that records structured task/run reports, uploads them to Google Drive with `rclone`, and verifies uploads using MD5/read-back.
- Capture integrations for OpenCode (plugin) and AGY (Stop hook).
- An optional v1.1 dispatcher that polls a private GitHub Issue inbox, validates explicit project/model/session requests, runs OpenCode in the configured workspace, captures the report through the core, verifies the Drive upload, and publishes the direct report URL to the originating issue.
- An optional v1.2 P1 Telegram notification outbox that reports verified delivery, attention states, and a copy-ready Arabic ChatGPT prompt without coupling notification failure to report delivery.
- An opt-in C1 secure coding execution foundation: per-project policy, dedicated non-root child identity, allowlisted child environment, dedicated HOME/XDG/model auth, and canonical clean-workspace validation.
- An opt-in C2 coding workflow built on C1: disposable per-task clones, trusted test profiles, diff/secret/path validation, one controlled commit/branch, exactly one Draft PR, durable recovery, and verified report/Issue/Telegram delivery. C2 is disabled by default and production coding tasks are not enabled.
- No database server, web dashboard, inbound public service, SaaS hosting, or mandatory cloud account.

## Sources of truth

| Topic | Canonical location |
| --- | --- |
| Product overview and quick start | `README.md` |
| Architecture and runtime boundaries | `docs/ARCHITECTURE.md` |
| Dispatcher behavior, state machine, security | `docs/DISPATCH.md` |
| C1 secure coding foundation and staging acceptance | `docs/SECURE-CODING.md` |
| AI-assisted setup | `docs/AI-QUICKSTART.md` |
| Acceptance evidence | `docs/ACCEPTANCE.md` |
| Failure recovery | `docs/TROUBLESHOOTING.md` |
| User-facing commands | `lib/agent_handoff/cli.py` (`agent-handoff --help`) |
| Configuration examples | `config/config.example.json`, `config/config.dispatch.example.json` |
| Tests | `tests/` |

Do not invent commands, config fields, model IDs, or guarantees. If documentation and code disagree, inspect the code and report the discrepancy; do not silently choose one.

## Capabilities

- Local, upload-verified report capture for OpenCode and AGY.
- Explicit multi-project mapping.
- Optional GitHub Issue dispatch with durable execution, upload, and publication reconciliation.
- Optional, disabled-by-default Telegram notifications with a durable local outbox; notification failure never changes verified delivery.
- Read-only OpenCode execution by default.
- Repeatable install, upgrade, rollback, doctor, and config validation.
- Deterministic tests with fake GitHub, fake OpenCode, and fake rclone.

## Explicit non-capabilities

- No AGY dispatcher.
- No Claude Code, Gemini CLI, or Codex CLI execution adapters.
- No SAME SESSION continuation; it is rejected, not simulated.
- No automatic project inference or automatic model selection.
- No public HTTP API, dashboard, database server, containers, or SaaS hosting.
- No automatic deployment of unrelated applications.
- No generic notification platform, bot provisioning, Telegram login, or guaranteed exactly-once Telegram delivery.
- No production C2 enablement, OS account creation, provider-key creation, workspace provisioning, deployment, auto-merge, or Draft PR automation without an explicit owner-configured coding policy.
- No full kernel/network sandbox for coding or tests; UID/GID isolation is not a complete sandbox.

## Safe setup sequence

Use generic paths. Do not copy another user's home directory, Drive IDs, GitHub login, or model preferences.

```bash
git clone https://github.com/OWNER/agent-handoff.git ~/src/agent-handoff
python3 ~/src/agent-handoff/setup/install.py --home ~/agent-handoff
agent-handoff auth
agent-handoff project add --name ProjectA --path ~/projects/project-a
agent-handoff project add --name ProjectB --path ~/projects/project-b
agent-handoff doctor
agent-handoff config validate
```

For a non-standard location use `--home` and `--bin-link`. The installer preserves `config/config.json`, runtime data, queue, state, logs, and credentials.

## Dispatcher setup

1. Confirm `gh auth status` works for your own account.
2. Confirm the private repository exists and has Issues enabled.
3. Discover exact model IDs with `opencode models`.
4. Add a `dispatch` section to the runtime config. Keep secrets out of the repository.
5. `agent-handoff config validate`
6. `agent-handoff dispatch run --once`
7. For continuous operation, use a systemd **user** service as shown in `docs/DISPATCH.md`. Never enable a persistent service without explicit user consent.
8. Submit a request using the exact metadata block from `docs/DISPATCH.md`.

The dispatcher is opt-in. Core capture must keep working when `dispatch.enabled` is false.

## Issue contract

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

Add `task: coding` only when the selected project has an explicit owner-enabled coding policy. Coding remains disabled by default. Issue text can never select commands, tests, UID/GID, repository, base branch, workspace, credentials, or isolation settings. See `docs/SECURE-CODING.md`.

- Issue text is untrusted. It can select only configured aliases, never paths, commands, tokens, or arbitrary models.
- Unknown fields, projects, agents, models, versions, and session modes are rejected.
- Only `allowed_users` may submit tasks.
- The repository must be private.
- A claimed or started request is never automatically executed again after a crash or restart.

## Safety and configuration

- Never commit `config/config.json`, rclone config, tokens, `.env` files, SSH keys, or live Drive/GitHub identifiers.
- Never paste credentials into an issue, PR, chat, log, or report.
- Validate paths before dispatch. The dispatcher requires a dispatch workspace that exactly matches one of the Agent Handoff project paths.
- Do not modify project source code during a read-only smoke test.
- Do not weaken `read_only` permissions unless the user deliberately configures `workspace_write` and `allow_write`.
- Do not run persistent background processes or enable systemd services during tests.
- Keep Telegram disabled unless deliberately configured. The bot token belongs only in the environment variable named by `notifications.telegram.token_env`; never in config, reports, logs, or Issues.

## Tests

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Use only evidence you actually produced. Do not claim a test, live session, Drive upload, or deployment succeeded unless you ran it and can cite the exact command and result. Mark everything else `Not verified`.

## Report requirements

When producing a report through Agent Handoff, separate facts:

- task/run identity,
- agent outcome,
- test evidence,
- deployment evidence,
- upload verification,
- GitHub publication.

A task outcome does not prove tests or deployment succeeded. An upload is verified only after the established MD5/read-back contract succeeds. A Drive folder existing is not upload verification. A GitHub comment existing is not upload verification.

## Upgrade and rollback

```bash
git -C ~/src/agent-handoff pull --ff-only
python3 ~/src/agent-handoff/setup/upgrade.py --home ~/agent-handoff
python3 ~/src/agent-handoff/setup/upgrade.py --home ~/agent-handoff --rollback latest
```

Upgrade and rollback preserve runtime reports, dispatch ledger, queue, config, logs, and credentials. Legacy snapshots without ownership manifests are refused rather than overlaid.

## Boundaries

- Do not merge a Draft PR without explicit human approval.
- Do not push to unrelated repositories.
- Do not add speculative adapters or infrastructure.
- Do not claim AI assistants can autonomously install the tool unless they actually have shell/GitHub/Drive permissions.
- If a required credential or permission is unavailable, stop and report the limitation accurately.
