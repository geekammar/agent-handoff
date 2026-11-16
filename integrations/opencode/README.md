# OpenCode integration

The active OpenCode integration is a native plugin installed at:

```text
~/.config/opencode/plugin/agent-handoff.js
```

Source of truth: `integrations/opencode/agent-handoff.js`.

## Verified API surface

Tested with OpenCode 1.19.5, in both `opencode run` and interactive TUI sessions:

- `chat.message` plugin hook for prompt capture.
- `event` plugin hook for `session.idle`, `session.error`, and `message.updated`.
- `input.client.session.get({ path: { id }, query: { directory } })`.
- `input.client.session.messages({ path: { id }, query: { directory } })`.

The plugin writes a short-lived mode-600 JSON payload to `$AGENT_HANDOFF_HOME/tmp/` and invokes:

```text
agent-handoff auto-complete --payload-file <payload> --json
```

The payload is removed after the call. Subagent sessions are skipped by checking `session.parentID`. Duplicate finalizations are suppressed by assistant message ID.

## Installation

`setup/install.py` copies the plugin and registers `$AGENT_HANDOFF_HOME/integrations/AGENT-INSTRUCTIONS.md` in `~/.config/opencode/opencode.json`. Existing OpenCode settings are preserved and backed up before an edit.

OpenCode must not be started with `--pure` if external plugins are expected.

## Environment overrides

- `AGENT_HANDOFF_HOME`: runtime home used by the plugin.
- `AGENT_HANDOFF_BIN`: explicit CLI executable path.

Do not replace this working plugin with a hypothetical implementation. Validate future changes against the installed OpenCode plugin API and real sessions.
