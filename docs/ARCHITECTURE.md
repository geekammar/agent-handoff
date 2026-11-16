# Architecture

## Goals

Agent Handoff records what an agent did, separately from whether tests or deployment succeeded, and verifies that each report reached Google Drive. The design favors a small standard-library Python core, atomic filesystem operations, and explicit integration boundaries.

## Runtime separation

Source code and runtime data are separate:

- Source checkout: any Git working tree, for example `/root/projects/agent-handoff`.
- Runtime home: `$AGENT_HANDOFF_HOME`, default `/root/agent-handoff`.
- Runtime state: `data/`, `queue/`, `state/`, `locks/`, `logs/`, `tmp/`, and `config/config.json`.
- External credentials: the rclone configuration and OAuth token, normally under `~/.config/rclone/`.

The installer copies only tracked source files into the runtime home. It never copies Git-ignored files or touches runtime state.

## Components

### Core CLI (`lib/agent_handoff`)

- `cli.py`: argument parsing and command handlers.
- `config.py`: defaults, runtime config, project name/path resolution, auto-registration.
- `report.py`: required-section enforcement and report rendering.
- `store.py`: task/run directories, atomic JSON/Markdown writes, session map, indexes, queue items.
- `upload.py`: rclone command wrapper, hierarchy checks, upload, MD5/read-back verification, retry backoff, worker lock.
- `util.py`: locks, atomic writes, IDs, redaction, command execution.
- `adapters/common.py`: shared automatic-session ingest pipeline.
- `adapters/agy.py`: AGY transcript parser and `Stop` hook adapter.

### OpenCode integration

The active integration is the installed OpenCode plugin `~/.config/opencode/plugin/agent-handoff.js`. The source copy lives in `integrations/opencode/agent-handoff.js`.

The plugin uses the OpenCode 1.19.5 plugin event surface:

- `chat.message` captures prompt text and model metadata.
- `session.idle` finalizes the session.
- `client.session.get` and `client.session.messages` retrieve session metadata and messages.
- Subagent sessions are skipped by checking `parentID`.

The plugin writes a mode-600 JSON payload under the runtime `tmp/` directory and invokes `agent-handoff auto-complete --payload-file ...`. The payload is deleted afterward. The plugin never uploads payloads or terminal transcripts.

### AGY integration

AGY 1.5.9 exposes a plugin command surface, but the verified, working integration in this installation is the global lifecycle hook:

- Hook configuration: `~/.gemini/config/hooks.json`
- Event: `Stop`
- Command: `/usr/local/bin/agent-handoff hook agy`
- Instructions: `~/.gemini/config/AGENTS.md`

The hook always exits successfully and prints `{}`. The adapter parses the transcript for exact user inputs and completed planner responses, derives an outcome, and calls the same ingest pipeline as OpenCode. Packaging is documented in `integrations/agy/`.

### Storage and queue

Tasks and runs are timestamped with cryptographic random suffixes. Runs never share a directory. A global `flock` serializes task creation, session maps, index generation, and queue mutation. A non-blocking worker lock prevents concurrent upload processes.

Queue items are separate atomic JSON files. A failed upload never deletes the report. A run is marked upload-verified only after every uploaded file passes MD5 or read-back verification. The remote `PROJECT-STATUS.md` is queued only after a run is verified and lists verified runs only.

## Data flow

```text
OpenCode plugin ─┐
AGY Stop hook ───┼─> agent-handoff auto-complete
manual CLI ──────┘        │
                         ├─> local task/run directories
                         ├─> PROJECT-STATUS.md
                         └─> upload queue
                                  │
                                  └─> rclone copyto + md5/read-back verification
```

## Upgrade model

`setup/upgrade.py` snapshots the installed code to `$AGENT_HANDOFF_HOME/releases/<timestamp>-pre-upgrade/`, runs `setup/install.py`, and restores the snapshot automatically if installation fails. Rollback creates a pre-rollback snapshot, restores the selected release, and re-runs the installer. Runtime state and configuration are never part of the snapshot.
