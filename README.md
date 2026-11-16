# Agent Handoff

Structured, upload-verified execution reports for OpenCode and AGY. Reports are stored locally and copied to an existing Google Drive reports subtree using `rclone`.

The runtime is deliberately small: Python standard library plus `rclone`. There is no database, web server, dashboard, daemon, npm package, or PyPI dependency.

This repository is the source distribution of the tested Agent Handoff v1.0 installation. Runtime data and credentials are intentionally kept outside Git.

## Repository layout

```text
agent-handoff/
  bin/agent-handoff                 # executable wrapper
  lib/agent_handoff/                # core Python CLI package
    cli.py                          # command surface
    config.py                       # runtime configuration and project mapping
    report.py                       # report templates and required sections
    store.py                        # atomic local task/run/report storage
    upload.py                       # rclone queue, upload, verification
    adapters/
      common.py                     # shared session-ingest pipeline
      agy.py                        # verified AGY Stop-hook adapter
  integrations/
    AGENT-INSTRUCTIONS.md           # rules injected into agents
    opencode/agent-handoff.js       # OpenCode plugin (installed copy is active)
    agy/                            # AGY hook example and capability notes
  tests/
    test_unit.py
    test_integration.py
    test_install.py
    acceptance.py                   # live end-to-end acceptance suite
    fake-rclone                    # deterministic Drive simulator
  setup/
    install.py, install.sh          # idempotent installer
    upgrade.py, upgrade.sh          # snapshot + upgrade
    rollback.sh                     # restore previous snapshot
    gdrive-auth.sh                  # headless-safe authorization helper
  docs/
    ARCHITECTURE.md
    ACCEPTANCE.md
    TROUBLESHOOTING.md
  config/config.example.json        # no credentials, no live folder IDs
```

## Requirements

- Python 3.10 or newer
- `rclone` for Google Drive uploads
- Optional: OpenCode and/or AGY for automatic capture
- No pip packages are required at runtime

The verified v1.0 environment used Python 3.14.4, rclone 1.60.1-DEV, git 2.53.0, OpenCode 1.19.5, and AGY 1.5.9.

## Install

Clone the private repository, then run the installer. The default runtime home is `/root/agent-handoff`; the source checkout and runtime home are separate so runtime reports, queues, state, and credentials are never part of the repository.

```bash
git clone git@github.com:geekammar/agent-handoff.git /root/projects/agent-handoff
python3 /root/projects/agent-handoff/setup/install.py --home /root/agent-handoff
```

The installer:

- synchronizes tracked source files into the runtime home,
- creates or updates `/usr/local/bin/agent-handoff` as a symlink,
- installs the OpenCode plugin and instruction registration,
- installs the AGY `Stop` hook and instruction block,
- backs up any configuration file before changing it,
- never touches `data/`, `queue/`, `state/`, `locks/`, `logs/`, `tmp/`, `config/config.json`, or the rclone configuration.

It is idempotent: identical files are not rewritten, and duplicate plugin, hook, or instruction entries are not created.

For a non-standard location:

```bash
AGENT_HANDOFF_HOME=/opt/agent-handoff \
  python3 setup/install.py --home /opt/agent-handoff --bin-link /usr/local/bin/agent-handoff
```

## Configuration

Runtime configuration lives at `$AGENT_HANDOFF_HOME/config/config.json` and is created with mode `600` on first use. It is not tracked by Git. Start from `config/config.example.json` and fill in the Drive parent/root folder IDs and project mappings, or use the CLI:

```bash
agent-handoff project add --name "Project" \
  --path /root/projects/project \
  --folder-id "<verified-drive-folder-id>"
```

A project is data, not code. The command verifies that a supplied folder ID is a direct child of the configured reports root before saving it. If no folder ID is supplied, it reuses exactly one existing child folder or creates one after verifying the root.

## Google Drive authorization

Authorization is the only manual setup step. The utility never prints rclone secrets or token contents.

```bash
agent-handoff auth
agent-handoff auth --check
agent-handoff doctor
```

Headless-safe import:

1. On a browser-capable machine: `rclone authorize "drive"`.
2. Sign in with the account that owns the Drive subtree.
3. Save the JSON token as a mode-600 file on the VPS. Do not paste it into chat.
4. Import it:

```bash
agent-handoff auth --import-token-file /root/gdrive-token.json
agent-handoff doctor
```

The import backs up the previous rclone config, writes only the configured remote, verifies Drive, and restores the previous config if verification fails. The helper is `setup/gdrive-auth.sh`.

Drive safety rules:

- Parent and reports-root IDs must resolve to the configured hierarchy before upload.
- Uploads use ID-anchored paths: `gdrive,root_folder_id=<project-folder-id>:`.
- `rclone sync` is never used. The utility uses `copyto`, `mkdir`, `lsf`, `lsd`, `md5sum`, and read-back verification.
- Only generated report files and `PROJECT-STATUS.md` are uploaded.

## Daily usage

```bash
agent-handoff doctor
agent-handoff list --project Project
agent-handoff retry-upload
```

Automatic capture:

- OpenCode: the plugin listens for `chat.message` and `session.idle`, captures the first prompt and final assistant response, skips subagent sessions, and calls `agent-handoff auto-complete`.
- AGY: the global `Stop` hook reads the conversation ID, transcript path, termination reason, and error state, then writes a task/run report. It always prints `{}` and exits 0 so reporting cannot block the agent.

Manual structured flow:

```bash
agent-handoff init --project Project --agent opencode \
  --title "Fix login redirect" --prompt-file /path/to/prompt.txt
agent-handoff prepare-run --task-id <task-id>
# write RUNS/<run-id>/REPORT.md
agent-handoff complete --task-id <task-id> --run-id <run-id> \
  --outcome succeeded --tests passed --tests-details "exact command and result"
```

## Report model

```text
<Project>/
  PROJECT-STATUS.md
  TASKS/
    <task-id>-<slug>/
      PROMPT.md
      META.json
      RUNS/
        <run-id>/
          PROMPT.md
          REPORT.md
          COMPLETE.json
```

Every `REPORT.md` contains the required sections, including Executive Summary, Task ID and Run ID, Agent and Session, Implemented Changes, Fixed Issues, Tests Passed/Failed/Not Run, Deployment Status, GitHub Branch/Commit/PR, Remaining Issues, Blockers, Recommended Next Steps, Architecture Changes, and Evidence links. Automatic reports add Verification Notes and never claim tests or deployment succeeded without evidence.

`PROJECT-STATUS.md` is a report index. Local indexes list all runs; uploaded indexes list upload-verified runs only and contain no local filesystem paths.

## Tests

Unit, integration, and regression tests use only the standard library and the fake rclone backend:

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

The live acceptance suite additionally verifies real OpenCode and AGY sessions and the real Google Drive hierarchy. It reads live folder IDs from the production config and optional `tests/live-config.json` (ignored by Git):

```bash
cp tests/live-config.example.json tests/live-config.json
# fill in chat/muse folder IDs and a local git project path, then:
python3 tests/acceptance.py
```

See `docs/ACCEPTANCE.md` for the recorded baseline and post-refactor results.

## Upgrade and rollback

Upgrade from a checked-out source tree:

```bash
git -C /root/projects/agent-handoff pull --ff-only
python3 /root/projects/agent-handoff/setup/upgrade.py --home /root/agent-handoff
```

The upgrade:

- snapshots the currently installed code under `$AGENT_HANDOFF_HOME/releases/`,
- runs the same idempotent installer,
- automatically restores the snapshot if installation fails,
- never touches runtime reports, queue, state, logs, config, or rclone authorization.

Rollback to the latest snapshot:

```bash
python3 /root/projects/agent-handoff/setup/upgrade.py --home /root/agent-handoff --rollback latest
# or: setup/rollback.sh
```

List snapshots:

```bash
python3 /root/projects/agent-handoff/setup/upgrade.py --home /root/agent-handoff --list
```

## Security

- Credentials, rclone configuration, tokens, `.env` files, and OAuth data are never committed.
- Runtime reports, prompt archives, queue items, state, logs, locks, and generated indexes are ignored by Git.
- The repository contains no live Google Drive folder IDs or user-specific project paths in defaults or tests.
- Uploaded report copies redact obvious secrets; the local copy remains intact for audit.
- Config and data files are mode `600`; runtime directories are mode `700`.
- Logs contain operational errors and paths, not tokens or rclone configuration contents.
- The private repository must never be made public without a dedicated data review.

## Troubleshooting and acceptance

- `docs/TROUBLESHOOTING.md`
- `docs/ACCEPTANCE.md`
- `agent-handoff doctor --json`

## Known limitations

- The rclone OAuth token can expire or be revoked.
- Automatic reports cannot independently verify code changes, tests, or deployments.
- OpenCode creates a report for every top-level session; v1 has no per-session opt-out.
- AGY `Stop` events occur per completed turn; each turn becomes a run under the conversation task.
- The reporting system does not inspect repository diffs or run tests itself.
- Drive folder IDs must remain stable; renaming a project in config does not rename the Drive folder.
