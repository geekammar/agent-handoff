# Secure coding execution foundation (C1)

C1 adds an **opt-in** security and execution foundation for future coding tasks submitted through the Agent Handoff dispatcher. It does **not** enable coding tasks in production and does **not** implement Draft PR automation.

The final coding workflow (isolated task worktrees, test execution, commit/branch creation, and Draft PR publication) is C2. Until C2 is reviewed and accepted, production projects such as Rabet and Yakfi remain `read_only`, isolation stays disabled in production, and no customer workspace is used for coding.

## Why coding needs isolation

The legacy dispatcher runs as the process owner. On the production host that is root, and the OpenCode child inherits that identity and most of the parent environment. Read-only is an OpenCode tool-permission policy, not a kernel access-control boundary. A coding task that can edit files and run tests therefore needs:

- an explicit non-root Unix identity;
- a child environment that does not contain the parent's Telegram, GitHub, rclone, or runtime secrets;
- a dedicated HOME/XDG context with its own model authentication;
- a workspace whose ownership, location, Git state, and access rights are validated before execution;
- fail-closed behavior when any of these cannot be proven.

C1 implements that foundation for the existing OpenCode adapter. The parent dispatcher remains root, and the parent continues to own the GitHub transport, Drive upload, ledger, and Telegram credentials.

## C1 scope

Implemented:

- per-project permission policy with safe inheritance;
- dedicated unprivileged child identity configuration and resolution;
- `subprocess` UID/GID drop with empty supplementary groups;
- allowlisted child environment;
- dedicated HOME and XDG directories;
- isolated model-authentication location;
- canonical workspace, ownership, access, protected-path, symlink, denied-project, and clean-Git validation;
- deterministic regression tests;
- staging acceptance instructions.

Not implemented:

- creating the Unix account, provider key, or workspace directories;
- a second daemon or a changed production systemd unit;
- container, namespace, seccomp, or full OS sandboxing;
- CPU/memory/disk quotas;
- Draft PR automation, commit/push policy, or test execution;
- enabling coding tasks through the production dispatcher.

## Per-project execution policy

`dispatch.execution` remains the global default. A project may add an `execution` override under its dispatch entry:

```json
{
  "dispatch": {
    "projects": {
      "ProjectA": {
        "workspace": "/srv/agent-handoff/work/project-a"
      },
      "ProjectB": {
        "workspace": "/srv/agent-handoff/work/project-b",
        "execution": {
          "permission_profile": "workspace_write",
          "allow_write": true,
          "isolation": {"enabled": true}
        }
      }
    }
  }
}
```

Resolution rules:

1. A missing project override inherits the global policy exactly.
2. A project-level `workspace_write` requires an explicit `allow_write: true` in the **same project override**. Inheriting write permission is not enough.
3. Write-capable execution additionally requires effective isolation. A project that resolves to `workspace_write` with `allow_write: true` while isolation is disabled is rejected; the dispatcher never falls back to a root write-capable child.
4. A project-level `read_only` is a safe downgrade and forces `allow_write` to false.
5. `allow_write: true` with any profile other than `workspace_write` is rejected.
6. Unknown project execution fields, unknown project isolation fields, and non-boolean `allow_write`/`isolation.enabled` values are rejected.
7. `execution.isolation` selection is per project: missing values inherit the global `enabled` default, an explicit `true` opts in, and an explicit `false` opts out (permitted only for read-only projects).
8. The effective policy is resolved once and used by config validation, the runner, and the OpenCode adapter. The effective isolation is persisted in the dispatch ledger.
9. Issue text cannot select or change a permission profile, workspace, UID, GID, isolation, model, or environment.

Example:

```json
{
  "dispatch": {
    "execution": {
      "permission_profile": "read_only",
      "allow_write": false
    },
    "projects": {
      "ProjectA": {"workspace": "/srv/agent-handoff/work/project-a"},
      "ProjectB": {
        "workspace": "/srv/agent-handoff/work/project-b",
        "execution": {
          "permission_profile": "workspace_write",
          "allow_write": true
        }
      }
    }
  }
}
```

ProjectA is read-only. ProjectB is write-capable only because both values are explicit. Read-only and write mode are still OpenCode tool policies, not kernel permissions.

## Dedicated Unix identity

The dedicated identity and workspace-root definition live under `dispatch.execution.isolation`. The global `enabled` value is the legacy default for every project, and each project may override it with `execution.isolation.enabled`. This allows one mixed configuration where legacy read-only projects stay non-isolated while a dedicated project opts into isolated write execution. Isolation is disabled by default:

```json
{
  "dispatch": {
    "execution": {
      "isolation": {
        "enabled": true,
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
    }
  }
}
```

Rules:

- `user` must exist locally and must not be root.
- The primary group must not be root, and the user must not belong to `root`, `sudo`, `wheel`, `admin`, `adm`, `docker`, `disk`, or `lxd`.
- The configured group, when present, must be the user's primary group in C1.
- The child is launched with the resolved numeric UID/GID and an empty supplementary-group list.
- The dispatcher must run as root to apply this drop. A non-root dispatcher fails closed; it never falls back to the parent identity.
- The identity, directories, auth file, and workspace ownership are validated before execution.
- No OS account or group is created by this repository.

Suggested staging layout:

```text
/srv/agent-handoff/
  home/agent-handoff-dev/          # owned by agent-handoff-dev:agent-handoff-dev
    .config/
    .local/share/opencode/auth.json
    .cache/
    tmp/
  work/
    agent-handoff/                 # clean Git workspace, owned by agent-handoff-dev
```

The runtime home (`$AGENT_HANDOFF_HOME`), `/root`, and protected system directories must not be used as child home or workspace. Ancestors ABOVE the dedicated leaf directories (for example `/srv/agent-handoff` and `/srv/agent-handoff/work`) must be root-owned and not group/world writable; only the leaf home/work directories are owned by the child identity.

## OpenCode executable trust

The isolated child must not be able to replace the executable that the privileged dispatcher starts. Before launch, the resolved OpenCode binary must:

- be a regular, non-setuid file;
- be owned by root (or the dispatcher identity) and not group/world writable;
- be executable by the dedicated identity;
- not live under the child's HOME, workspace, cache, or TMPDIR;
- have root/dispatcher-owned ancestors that are not group/world writable, except for sticky root-owned directories such as `/tmp`.

`shutil.which` is restricted to the reviewed `child_path`. An explicit `AGENT_HANDOFF_OPENCODE_BIN` is accepted only after the same checks. A user-home installation, a workspace-local shim, or a world-writable directory fails closed instead of being executed.

## Child model authentication

The child receives `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, and `TMPDIR` from the configured identity. OpenCode authentication is expected at:

```text
$XDG_DATA_HOME/opencode/auth.json
```

C1 requirements:

- The file must exist before isolated execution.
- It must be owned by the dedicated UID and must not be group/world readable.
- It must not be a symlink.
- It must not live inside the coding workspace.
- Root's OpenCode authentication is never copied, read, or inherited.
- The provider credential should be independent, with a limited quota or spending cap.
- Never print the file or its values in logs, reports, tests, or chat.

C1 does not create a real provider key or log in OpenCode. The owner approves that provisioning in a separate staging operation.

## Child environment allowlist

Isolated execution builds a new environment instead of copying the parent. The child receives only:

| Variable | Source |
| --- | --- |
| `PATH` | Reviewed `child_path`, system directories by default |
| `HOME`, `USER`, `LOGNAME` | Dedicated identity |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `TMPDIR` | Dedicated identity home |
| `PWD` | Canonical workspace |
| `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` | Safe locale values copied from the parent, when present |
| `OPENCODE_CONFIG_CONTENT` | Minimal read-only or explicit write policy |
| `OPENCODE_DISABLE_AUTOUPDATE` | `1` |

The child does not receive `AGENT_HANDOFF_TELEGRAM_TOKEN`, `AGENT_HANDOFF_HOME`, `GH_TOKEN`, `GITHUB_TOKEN`, `GH_HOST`, `GITHUB_TOKEN_PATH`, `RCLONE_CONFIG`, `RCLONE_CONFIG_PASS`, model-provider API keys from the parent, or arbitrary `AGENT_HANDOFF_*`, `GH_*`, `GITHUB_*`, or `RCLONE_*` variables. This is an allowlist, not a denylist.

The parent process is unchanged: it can still use Telegram, GitHub, rclone, and the durable ledger. `os.environ` is never modified globally.

The legacy non-isolated adapter keeps its previous behavior for backward compatibility, including the existing notification-token filter.

## Workspace safety

An isolated coding workspace must:

- be an absolute, canonical path;
- contain no symlink components or symlink escape;
- live under one of `dispatch.execution.isolation.workspace_roots`;
- be outside `/root`, `$AGENT_HANDOFF_HOME`, and protected system directories;
- not use a denied project name or path component (`Rabet` and `Yakfi` are always denied by default);
- exist, be owned by the dedicated UID/GID, and be readable/executable by it;
- be writable only when the effective project policy is explicitly `workspace_write`;
- not be group/world writable;
- have trusted ancestors: root/dispatcher-owned and not group/world writable (sticky root-owned directories are allowed);
- contain a real `.git` directory owned by the dedicated identity, not a symlink, not group/world writable, and not a linked-worktree pointer file;
- be a clean Git working tree at execution time.

Canonical-path and ownership checks narrow substitution risk, but they do not eliminate TOCTOU races between validation and use. The parent re-validates before each execution, the child is the only non-root writer, and the workspace must be a dedicated disposable path. A complete TOCTOU guarantee is not claimed.

A dirty tree is refused. C1 never resets, cleans, stashes, or overwrites local work. Repository config and hooks are not trusted by the privileged parent: the clean check runs as the dedicated identity with system/global config disabled, `core.fsmonitor` off, `core.hooksPath` disabled, `core.untrackedCache` off, and an empty external excludes file. Repository-local `.gitignore`/`.git/info/exclude` content can still hide intentionally ignored files, which is one reason C2 must use disposable task worktrees.

**A clean Git tree is not a complete isolation guarantee.** Repository content, build scripts, and test commands can still be malicious. C2 must prepare disposable clean worktrees, bound resources, and review the publication path.

## Process lifecycle

The existing lifecycle is preserved:

- bounded execution timeout;
- process-group termination with `SIGTERM`, then `SIGKILL`;
- `SIGINT`/`SIGTERM` cleanup in the dispatcher;
- progress heartbeats without prompt text or local paths;
- ledger claim before execution and no automatic re-execution after a crash.

Isolation adds a preflight before the `RUNNING` transition. Missing identity, missing auth, wrong ownership, protected path, denied project, dirty tree, or failed access validation marks the durable record `NEEDS_ATTENTION` without starting OpenCode and without falling back to root.

## Security limitations

1. Unix UID/GID isolation is not a complete sandbox. Kernel namespaces, seccomp, cgroups, and filesystem sandboxes are not implemented.
2. The parent dispatcher still runs as root. Its config, ledger, credentials, and service unit must be protected.
3. The child can read its own model-authentication file by design.
4. The child may have network access unless the host adds a separate network policy.
5. CPU, memory, and disk exhaustion are not bounded by C1. Resource limits and acceptance verification are required before unsupervised coding.
6. Prompt injection remains possible. Issue text is untrusted even after author and metadata validation.
7. `read_only` and `workspace_write` are OpenCode tool-permission policies, not kernel access-control mechanisms.
8. Running repository tests or modified build code is a separate risk. C1 does not execute tests; C2 must address it.
9. Clean Git status proves only that the current tree has no uncommitted/untracked changes. It does not prove that repository content is safe.
10. Git hooks are disabled for the privileged clean check, but code run later by the child can still be influenced by repository content. Repository-local ignore rules can also hide ignored files from the clean-status check.
11. Executable and workspace path checks reduce substitution risk but cannot prove the absence of a TOCTOU race. C2 must use disposable worktrees and a reviewed publication path.
12. Legacy read-only projects that do not opt into isolation keep the pre-C1 parent identity and environment behavior. A write-capable policy without effective isolation is rejected rather than silently executed as root.

## Staging acceptance (operator-controlled)

Do not perform these steps on production Rabet or Yakfi workspaces. Do not restart the production dispatcher service during C1.

1. Obtain explicit owner approval for the Unix account, group, home, auth location, and workspace.
2. Create `agent-handoff-dev` with no sudo/admin/privileged supplementary groups. Verify with `id agent-handoff-dev`; it must not list `root`, `sudo`, `wheel`, `admin`, `adm`, `docker`, `disk`, or `lxd`.
3. Create the home/XDG directories and workspace-owned layout. Set the auth file mode `600` and owner to the dedicated identity. Do not copy root's auth file.
4. Provision an independent provider credential with a quota/spend cap. Verify only its owner/mode, never its contents.
5. Clone a disposable test repository to `/srv/agent-handoff/work/agent-handoff`; confirm a clean tree.
6. In a **staging** config only, keep `dispatch.execution.isolation.enabled: false`, define the identity and workspace roots, and opt the disposable project in with `execution.isolation.enabled: true` plus `read_only`. Run:

   ```bash
   agent-handoff config validate --json
   ```

7. Submit one staging Issue. Verify from the host that the OpenCode child has the dedicated UID/GID, no supplementary groups, the dedicated HOME/XDG values, no parent secrets, and no root auth access.
8. Verify the read-only probe denies a requested file write. Verify report capture, MD5/read-back upload, receipt, and Issue publication are unchanged.
9. Test dirty-tree refusal by adding an untracked file to the disposable workspace; the task must become `NEEDS_ATTENTION` with no OpenCode run, and the file must remain.
10. Only then test `workspace_write` on the disposable repository with an explicit project override, `allow_write: true`, and `execution.isolation.enabled: true`.
11. Revert staging configuration or leave it isolated; do not enable production coding until C2 is accepted.

A live staging run is not proof of C2 automation. If a step cannot be performed safely, record it as `Not verified`.

## C1 versus C2

C1 provides configuration, identity, environment, workspace, and process-lifecycle safety. C2 must still provide:

- disposable clean task worktrees;
- controlled test execution;
- commit/branch creation and Draft PR publication;
- publication/rejection policy for untrusted changes;
- resource limits and host-level monitoring;
- acceptance evidence for a real isolated coding task.

No user can submit a coding task through production Agent Handoff yet.
