# AI-assisted quick start

This guide is for a developer who wants to hand the Agent Handoff repository URL to an AI assistant and have the assistant guide installation, configuration, and verification.

Agent Handoff is self-describing but not self-installing. An AI without shell, GitHub, and Drive access can explain and review commands, but it cannot perform the installation or claim that a report was uploaded.

## Universal prompt

Use this prompt with ChatGPT Work, Codex, Claude, Gemini, or another tool-enabled assistant. Replace `OWNER` only if you are using a fork:

```text
Read AGENTS.md, README.md, docs/AI-QUICKSTART.md, and docs/DISPATCH.md from
https://github.com/OWNER/agent-handoff. Help me install and verify Agent Handoff
on my own Linux machine. Use only commands documented in the repository. Do not
invent config fields, model IDs, or guarantees. Ask before enabling any persistent
service. Never ask me to paste tokens or rclone config contents into chat. If you
cannot run shell, GitHub, or Drive operations, give me the exact commands to run
and ask for only the non-secret output needed to verify each step.
```

## What the assistant needs

| Capability | Why |
| --- | --- |
| Read the repository | Discover canonical commands and contracts. |
| Shell on the target host | Clone, install, configure, run tests, and inspect state. |
| `gh` authenticated for the user | Verify a private Issue inbox and post delivery comments. |
| rclone Google Drive authorization | Upload and verify reports. |
| OpenCode with provider credentials | Execute dispatched tasks. |

The assistant must not ask for secrets in chat. Google Drive authorization uses `rclone authorize "drive"` on a browser-capable machine and a mode-600 token file on the target host. GitHub uses the user's own `gh auth login` session.

## Step 1 — Inspect the host

```bash
python3 --version
rclone version
opencode --version
gh --version
gh auth status
```

Minimum: Python 3.10+ and rclone. OpenCode is required only for automatic capture or dispatch. `gh` is required only for dispatch.

## Step 2 — Install

```bash
git clone https://github.com/OWNER/agent-handoff.git ~/src/agent-handoff
python3 ~/src/agent-handoff/setup/install.py --home ~/agent-handoff
agent-handoff --version
```

The installer preserves existing runtime config, data, queue, state, logs, and credentials. Do not copy an example config over `$AGENT_HANDOFF_HOME/config/config.json`.

## Step 3 — Authorize Drive and add projects

```bash
agent-handoff auth
agent-handoff project add --name ProjectA --path ~/projects/project-a
agent-handoff project add --name ProjectB --path ~/projects/project-b
agent-handoff doctor
agent-handoff config validate
```

`project add` verifies the Drive folder hierarchy. If Drive is not ready, complete authorization first. Do not use another user's folder IDs or paths.

## Step 4 — Prove core capture

```bash
agent-handoff init --project ProjectA --agent opencode \
  --title "Onboarding smoke" --prompt-file /path/to/prompt.txt
agent-handoff prepare-run --task-id <task-id>
# write RUNS/<run-id>/REPORT.md with real evidence
agent-handoff complete --task-id <task-id> --run-id <run-id> \
  --outcome succeeded --tests not_verified \
  --tests-details "No independent tests were run for this smoke task."
agent-handoff retry-upload --force --json
agent-handoff list --project ProjectA --json
```

Verify `upload.verified: true` in `COMPLETE.json`. A successful command is not upload verification by itself.

## Step 5 — Optional private Issue dispatcher

1. Create or choose a **private** GitHub repository with Issues enabled.
2. Confirm `gh auth status` uses the intended account.
3. Run `opencode models` and record exact provider/model IDs.
4. Add a `dispatch` section to the existing runtime config. Use `config/config.dispatch.example.json` only as a structural reference.
5. Ensure each dispatch workspace exactly matches the core project path.
6. Validate:

```bash
agent-handoff config validate
agent-handoff dispatch run --once --json
```

7. Create an issue from `.github/ISSUE_TEMPLATE/agent-handoff-task.md` and submit it.
8. Inspect delivery:

```bash
agent-handoff dispatch status --json
```

For continuous operation, use a systemd user service only after the user explicitly consents. The template is in `docs/DISPATCH.md`; it must be installed and enabled by the user, not by the assistant during tests.

## What the assistant must say when it cannot act

Use language like:

> I can guide the setup, but I cannot run shell commands, authenticate your GitHub account, or access your Google Drive from this chat. Please run the commands below and share only the non-secret summary output. I will not claim the upload succeeded until `COMPLETE.json` shows `upload.verified: true` with a direct Drive URL.

Do not claim:

- the tool is installed;
- a Drive report exists;
- an issue comment was published;
- tests or deployment passed;

unless the corresponding command was actually run and its result observed.

## Verification checklist

- [ ] `agent-handoff --version` prints the installed version.
- [ ] `agent-handoff doctor --json` has no unexplained failures.
- [ ] `agent-handoff config validate --json` is valid.
- [ ] `PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v` passes.
- [ ] At least one core report is upload-verified with a receipt.
- [ ] If dispatch is enabled, a private Issue request produced exactly one model execution and one delivery comment.
- [ ] `dispatch status` shows `ISSUE_UPDATED` only after upload verification and direct URL publication.
