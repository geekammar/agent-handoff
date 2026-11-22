---
name: Agent Handoff task
about: Dispatch an explicitly configured OpenCode task from a private repository
title: "[handoff] "
labels: ""
assignees: ""
---

<!-- agent-handoff-request -->
handoff-version: 1
project: ProjectA
agent: opencode
model: fast
session: new
# task: coding   # only for owner-enabled C2 projects; default is read_only
<!-- /agent-handoff-request -->

Describe the task here. Include only instructions for the selected project.
Do not include tokens, passwords, private keys, or environment values.

Constraints for this request:

- `project` must match a project configured in the dispatcher.
- `agent` must be `opencode`.
- `model` is an alias from the dispatcher's configured models.
- `session` must be `new`; SAME SESSION is not supported in v1.1.
- `task` is optional; `read_only` is the default. `coding` is rejected unless the owner enabled C2 for the project, and it never lets Issue text set commands, tests, paths, identity, repository, base branch, or isolation.
