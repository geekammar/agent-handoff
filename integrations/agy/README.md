# AGY integration

## Verified capabilities

Inspected on AGY 1.5.9:

- `agy --help` documents print mode, interactive mode, sessions, projects, and lifecycle hooks.
- `agy plugin list` reports no installed plugins.
- `agy plugin --help` documents plugin install/list/validate commands, but no AGY plugin is used by this installation.
- The working integration is the global lifecycle hook in `~/.gemini/config/hooks.json`.

The `Stop` hook was verified in real `agy -p` and interactive TUI sessions. It receives a JSON payload containing the conversation ID, transcript path, termination reason, `fullyIdle`, last user input, workspace paths, and error state.

## Installed hook

```json
{
  "agent-handoff-report": {
    "Stop": [
      {
        "type": "command",
        "command": "/usr/local/bin/agent-handoff hook agy",
        "timeout": 25
      }
    ]
  }
}
```

The hook always prints `{}` and exits 0. A reporting failure is written to `$AGENT_HANDOFF_HOME/logs/integration.log` and never blocks the agent.

## Adapter

The parser and outcome mapping live in `lib/agent_handoff/adapters/agy.py`. It:

- reads `USER_INPUT` entries with `source: USER_EXPLICIT`,
- strips AGY wrapper tags,
- reads completed `PLANNER_RESPONSE` entries,
- maps termination reasons to `succeeded`, `failed`, `blocked`, or `cancelled`,
- deduplicates repeated `Stop` events using a transcript/Stop fingerprint.

The adapter then calls the shared automatic ingest pipeline used by the OpenCode plugin.

## Installation

`setup/install.py` merges the `agent-handoff-report` entry into `~/.gemini/config/hooks.json` and replaces only the marked Agent Handoff block in `~/.gemini/config/AGENTS.md`. Other hooks and rules are preserved.

`hooks.example.json` is a reference copy. The installer generates the live config so the CLI path stays consistent.
