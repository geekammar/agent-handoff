<!-- AGENT HANDOFF REPORTING v1.0 -->
Agent Handoff reporting is installed and automatic.

- Do not create, copy, or upload handoff reports on your own. Do not run environment checks for handoff variables unless the operator explicitly asks.
- If the operator explicitly provides `AGENT_HANDOFF_TASK_ID` and `AGENT_HANDOFF_RUN_DIR`, write the structured report to `REPORT.md` in that run directory, then finalize with `agent-handoff complete`.
- Use only verified evidence. Never claim tests or deployment succeeded unless you ran them and can cite the exact command/result. Mark everything else `Not verified`.
- A process exit, an agent turn completing, tests passing, deployment, and report upload are separate facts. Do not conflate them.
- Never include secrets, `.env` contents, tokens, SSH keys, credentials, or full terminal transcripts in a report.
<!-- END AGENT HANDOFF REPORTING -->
