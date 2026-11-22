# Secure coding execution (C1 foundation and C2 workflow)

C1 adds an **opt-in** security and execution foundation for coding tasks submitted through the Agent Handoff dispatcher. C2 builds the coding workflow on top of it: disposable per-task clones, trusted test execution, diff validation, one commit, one branch, and one Draft Pull Request.

C2 is implemented in source but **disabled by default and not enabled in production**. Production projects such as Rabet and Yakfi remain `read_only`, production isolation stays disabled unless the owner explicitly provisions it, and no customer workspace is used for coding without owner-approved staging acceptance.

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

A dirty tree is refused. C1 never resets, cleans, stashes, or overwrites local work. Repository config and hooks are not trusted by the privileged parent: the clean check runs as the dedicated identity with system/global config disabled, `core.fsmonitor` off, `core.hooksPath` disabled, `core.untrackedCache` off, and an empty external excludes file. Repository-local `.gitignore`/`.git/info/exclude` content can still hide intentionally ignored files, which is why C2 uses disposable task clones.

**A clean Git tree is not a complete isolation guarantee.** Repository content, build scripts, and test commands can still be malicious. C2 uses disposable clean clones, bounded test time/output, diff validation, and a Draft-PR-only publication path.

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
8. Running repository tests or modified build code is a separate risk. C1 does not execute tests; C2 runs only owner-configured trusted profiles under the same non-root identity and bounds time/output, but tests can still be malicious.
9. Clean Git status proves only that the current tree has no uncommitted/untracked changes. It does not prove that repository content is safe.
10. Git hooks are disabled for the privileged clean check, but code run later by the child can still be influenced by repository content. Repository-local ignore rules can also hide ignored files from the clean-status check.
11. Executable and workspace path checks reduce substitution risk but cannot prove the absence of a TOCTOU race. C2 uses disposable clones and a parent-owned publication staging path, but residual TOCTOU risk is not eliminated.
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

## C2 coding workflow

C2 is enabled only by an explicit double opt-in:

1. `dispatch.coding.enabled: true` in trusted runtime configuration; and
2. `coding.enabled: true` on the selected dispatch project.

The project must have effective isolation enabled with a validated non-root identity. A missing value, a disabled project, or any unavailable precondition fails closed; there is no root fallback.

### Coding request contract

The Issue metadata block gains one optional field:

```text
task: coding
```

The default is `read_only`. Unknown values are rejected. Issue text may contain the task description, acceptance criteria, and feature details, but it cannot select or change:

- Unix user/group/home;
- credentials or token environment names;
- repository, base branch, or owner-controlled model aliases;
- test commands, test profiles, or the test environment;
- workspace or staging paths;
- isolation, protected paths, allowlists, limits, or publication policy.

### Trusted coding configuration

Global defaults live under `dispatch.coding`; per-project policy lives under `dispatch.projects.<Name>.coding`:

```json
{
  "dispatch": {
    "coding": {
      "enabled": true,
      "staging_root": "/srv/agent-handoff/state/coding-publication",
      "limits": {
        "test_timeout_seconds": 900,
        "max_files_changed": 25,
        "max_diff_bytes": 500000,
        "max_file_bytes": 200000,
        "max_output_bytes": 50000,
        "max_workspace_bytes": 2000000000,
        "max_coding_tasks": 1,
        "max_publication_attempts": 3,
        "max_test_memory_bytes": 2000000000,
        "max_test_processes": 128,
        "max_test_file_bytes": 256000000
      },
      "publication": {
        "draft": true,
        "branch_prefix": "agent-handoff/",
        "max_attempts": 3,
        "pr_title_prefix": "Agent Handoff: ",
        "token_env": null
      },
      "protected_paths": [".github/workflows/**", ".gitmodules", ".env", ".env.*"],
      "secret_scan": true,
      "test_profiles": {
        "python": {
          "commands": [["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]],
          "timeout_seconds": 900,
          "env": {"PYTHONPATH": "lib", "PYTHONHASHSEED": "0"}
        }
      }
    },
    "projects": {
      "ProjectB": {
        "workspace": "/srv/agent-handoff/work/project-b",
        "execution": {
          "permission_profile": "workspace_write",
          "allow_write": true,
          "isolation": {"enabled": true}
        },
        "coding": {
          "enabled": true,
          "repository": "owner/project-b",
          "base_branch": "main",
          "workspace_root": "/srv/agent-handoff/coding/project-b",
          "test_profile": "python",
          "allowed_model_aliases": ["fast"],
          "allowed_paths": [],
          "protected_paths": [".github/workflows/**"],
          "cleanup_on_success": true
        }
      }
    }
  }
}
```

Rules:

- `publication.draft` must remain `true`; C2 never creates a ready-for-review or merged PR.
- `token_env`, when set, names a parent-only environment variable containing the GitHub publication token. Otherwise the dispatcher asks the configured `gh` command for its token. The token is never placed in the child environment, ledger, report, or logs.
- `test_profiles` commands are argv arrays, never shell strings. Profile environment names are validated and credential-like names are rejected.
- `workspace_root` is a child-owned disposable root inside an isolation `workspace_roots` entry. It must be separate from the source checkout.
- `staging_root` is a parent-owned, non-child-accessible directory used for validated publication commits.
- `allowed_model_aliases` must list at least one configured dispatch alias.
- Unknown fields, invalid branches, invalid paths, unsafe prefixes, non-Draft publication, and missing isolation are configuration errors.

### Disposable workspace lifecycle

For each coding task:

1. Resolve the trusted source checkout from the project workspace and verify its `origin` matches the configured repository.
2. Resolve the configured base branch to a commit SHA.
3. Create a unique `issue-<number>-<task_id>` directory under `coding.workspace_root`; refuse to reuse an existing or dirty directory.
4. Clone the source into that directory as the dedicated non-root identity, detach at the verified base SHA, reject any symlink, and require a clean tree.
5. Run OpenCode only in this disposable workspace. The active developer checkout is only a source of committed Git objects.
6. On success and after verified report capture, remove the workspace when `cleanup_on_success` is true. Failed or blocked tasks preserve the workspace and durable evidence for review.
7. Cleanup refuses paths outside the managed root, symlinks, unexpected names, and directories not owned by the dedicated identity. A task cannot clean another task's directory: the expected `issue-<number>-<task_id>` basename is checked again at cleanup time.

Resume revalidates the persisted workspace before OpenCode or its artifacts are used:

- the original path is inspected with `lstat` before any `realpath`, so a symlink/path swap is rejected;
- the basename must equal the expected `issue-<number>-<task_id>`, and the canonical path must be a direct child of the managed root;
- directory UID/GID and private permissions, `.git` type/UID/GID/mode, origin identity, base ref, and base commit are verified;
- before the agent starts the tree must be clean; after a genuine interrupted task the workspace may be modified, but only when the persisted validated manifest still matches the actual files exactly;
- any mismatch blocks publication instead of rerunning the agent or adopting another task's workspace.

The publisher never runs Git hooks or repository-controlled filters with parent privileges. It verifies the configured repository identity through GitHub, checks the staging root and its ancestors are parent-owned and non-child-writable, creates a fresh parent-owned staging repository, fetches the base from the trusted GitHub repository, copies only digest-verified regular files (using `O_NOFOLLOW` and symlink-free destination parents), stages them explicitly, and commits with hooks disabled and `--no-verify`. Git global/system configuration, hooks, fsmonitor, replacement objects, pager, and credential helpers are disabled for privileged Git operations. `protocol.file.allow=always` is enabled only for the documented local workspace-clone path and tests, not for the publication fetch/push path.

Git authentication helpers are never placed inside the publication staging tree. For each authenticated Git operation the parent creates a fresh `.auth-*` directory under the validated staging root, creates `.askpass.sh` with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0700`, verifies its ownership/content/mode, and removes the directory in a `finally` block. The token is passed only as a short-lived environment variable to the trusted Git process; it is never written to disk, the staging tree, a report, a log, or the OpenCode/test child environment. `.askpass.sh`, `.git-credentials`, `.gitconfig`, `.netrc`, and `.auth-*` names are reserved and rejected in every validation manifest.

### Trusted test execution

Tests run in the same disposable workspace under the same dedicated non-root identity:

- owner-configured argv commands only; no shell interpolation from Issue text;
- bounded overall timeout and bounded stdout/stderr capture;
- process-group termination with `SIGTERM` then `SIGKILL`;
- allowlisted environment with no parent Telegram, GitHub, rclone, or runtime secrets;
- `RLIMIT_CPU` and `RLIMIT_NOFILE` where supported by the host;
- exit code, duration, timeout state, and truncated logs recorded;
- fail-fast on the first failing command.

If any trusted test fails or times out, C2 persists the failure evidence and the blocked state in one durable ledger transition. A mandatory test gate runs before diff validation, commit, push, and PR creation, and again on every resume. Only an explicit `tests.status == passed` with a matching configured profile, non-empty executed commands, verified zero exit codes, no timeout, and no orphaned process group satisfies the gate. `failed`, `timed_out`, `interrupted`, `orphans`, `error`, missing, unknown, or malformed evidence fails closed. A stale `TESTS_PASSED` milestone is cleared and cannot override failure evidence. C2 still generates and delivers an honest report, but never reruns the agent or publishes failing code automatically.

### Artifact integrity from validation to commit

`validate_changes()` records a SHA-256 digest, size, normalized publication mode, and raw mode for every approved regular file in a canonical manifest with its own digest. Validation and publication are separate process steps, so publication does not trust path names or Git status alone:

1. The publisher re-reads each source file with no-follow semantics, compares its digest, size, and modes to the manifest, and rejects any mismatch.
2. The exact bytes read from the source are written to the parent-owned staging tree; the destination is re-read and verified.
3. After `git add`, the staged index blob and mode are verified against the manifest.
4. After commit, the committed tree path set, blob content digest, and mode are verified against the manifest.
5. Deleted paths must be absent from the index and committed tree.
6. Copy (`C`) and rename (`R`) statuses are distinguished: a rename removes its source path, a copy must leave its source present and unchanged, and a modified copy source is validated as its own changed file. Protected copy sources are refused.
7. Staged and committed path sets are compared with rename detection disabled, so a rename is always verified as one deletion plus one addition against the exact manifest.
8. Unexpected staged or committed paths fail closed.

A concurrent mutation is a publication failure, not a warning. The agent and trusted-test process groups are checked after execution; a lingering group is terminated and the task is blocked from publication.

### Diff validation and publication

Before commit, C2 records changed paths and rejects:

- absolute paths, `..`, control characters, or `.git` metadata;
- protected paths and paths outside an optional allowlist. Mandatory protected paths always apply and configuration can only add patterns, never remove them: root-level and nested `.ssh`, `credentials`, `.env*`, `auth.json`, `rclone.conf`, `.askpass.sh`, `.gitconfig`, `.git-credentials`, `.netrc`, `.gitattributes`, keys/certificates, and `.git`/GitHub workflow metadata;
- symlinks, submodules, binary files, and unsupported Git modes;
- excess changed files, diff bytes, file sizes, or workspace size;
- obvious secret material (defense in depth; values are redacted from evidence).

The publisher validates the repository, branch namespace, base commit ancestry, commit parent, and committed path set. It pushes without force, refuses protected/default branches, verifies the remote branch SHA, and creates or recovers exactly one Draft PR by a stable body marker. Before reuse or a body update it requires the PR to be open, Draft, on the expected branch/base, and at the verified head SHA; a moved, closed, merged, non-Draft, or mismatched PR fails closed. After the report upload is verified, the dispatcher updates that same Draft PR body with the verified report link; an update failure is recorded as a warning and does not block Issue/report delivery. Multiple matching PRs, or an existing branch with a different commit, fail closed. Transient commit/push/PR failures use bounded retry/backoff. A lost staging repository is recovered by remote SHA when the branch already exists, or by safely rebuilding an unpublished commit when the remote branch is absent; an altered remote branch is never overwritten. Interrupted staging cleanup is retried from durable evidence.

### Durable stages and recovery

Coding records use the existing durable ledger plus `coding.stage`:

```text
REQUEST_ACCEPTED -> WORKSPACE_PREPARED -> AGENT_RUNNING -> AGENT_COMPLETED
  -> TESTING -> VALIDATING_DIFF -> COMMIT_PREPARED -> BRANCH_PUBLISHED
  -> PR_CREATED -> REPORT_CAPTURED -> UPLOAD_VERIFIED -> ISSUE_UPDATED
```

The record persists the task/run IDs, source issue, repository, base branch/commit, workspace, branch, commit SHA, PR number/URL, test result, diff evidence, publication attempts/backoff, and report identity. Milestones distinguish agent completion, tests passed, PR publication, Drive upload verification (`REPORT_VERIFIED`), Issue acknowledgement (`ISSUE_DELIVERED`), and acknowledged Telegram API delivery (`TELEGRAM_SENT`). A queued or ambiguous Telegram send is not marked sent. Reports and Issue comments are snapshots: steps not reached at snapshot time render `pending` or `not_run`, and `REPORT_DELIVERED` is retained only as a legacy alias of `ISSUE_DELIVERED`.

Recovery rules:

- a claimed request that never started OpenCode may resume workspace preparation; the workspace is revalidated with lstat-first identity/base/manifest checks before use;
- a commit created before its SHA was persisted may be safely recreated in a fresh parent-owned staging repository because no branch or PR has been published yet;
- a running OpenCode session with persisted events resumes tests/publication without rerunning the agent;
- a running session without recoverable output becomes `NEEDS_ATTENTION`;
- interrupted tests or diff validation rerun from the preserved workspace only if the mandatory test gate has explicit passing evidence; failing or missing evidence blocks instead of publishing;
- a prepared commit is reused; a pushed branch is recognized by remote SHA even if the publication staging directory disappeared; a created PR is recovered by branch/marker and verified open/Draft/head-SHA;
- milestones are reconciled from authoritative evidence after a restart: verified upload implies `REPORT_VERIFIED`, a published acknowledged Issue comment implies `ISSUE_DELIVERED`, an existing verified PR implies `PR_CREATED`/`PR_PUBLISHED`, and a durable `SENT` Telegram notification implies `TELEGRAM_SENT` without sending it again;
- a failed Draft PR body update after verified report upload is retried with bounded backoff against the same PR; it never creates another PR;
- upload, GitHub comment, and Telegram failures retry only their own stage;
- manual `dispatch deliver` and automatic publication share a per-Issue durable lock, so concurrent attempts produce at most one delivery comment;
- an existing published branch/PR is never replaced by a different code result.

### Security limitations

C2 does not provide a full sandbox. Residual risks include:

- Unix UID/GID separation is not kernel namespaces, seccomp, cgroups, or a filesystem sandbox;
- the parent dispatcher still owns GitHub, Drive, and Telegram credentials;
- the coding child and trusted tests may have network access unless the host adds a separate network policy;
- tests run repository code by design; a malicious or broken test can consume resources within the configured time/output bounds and can read the dedicated child's own model-auth file. Only trusted, reviewed test profiles should be configured;
- secret scanning and path rules are defense in depth, not proof that a diff contains no sensitive information;
- prompt injection remains possible even after author and metadata validation;
- `workspace_write` is an OpenCode tool policy, not a kernel permission;
- resource limits are bounded time/output/size checks, not complete CPU/memory/disk quotas; workspace size is checked after agent/tests, not enforced as a live quota;
- one dispatcher runner lock serializes coding work; `max_coding_tasks` is validated but C2 currently executes at most one coding task per dispatcher;
- Draft PR publication is a review artifact, not acceptance, deployment, or merge approval.
- Artifact digests, no-follow reads, and post-commit verification narrow the validation-to-publication race. They do not defend against a hostile root-level process, a compromised parent, or a separately detached child process that escapes the tracked process group.

### How to disable and recover

Coding is disabled when either `dispatch.coding.enabled` or the project `coding.enabled` is false. Setting both false leaves legacy read-only dispatch unchanged. Do not run `git reset`/`git clean` in a source checkout to recover a coding task; inspect `dispatch status --json`, the preserved disposable workspace, and the durable ledger stage. Use the normal dispatcher poll to resume safe stages. Use `dispatch deliver` only for report/comment reconciliation; it never runs an agent.

### Staging acceptance procedure

1. Obtain explicit owner approval for the Unix identity, dedicated model credential, workspace root, staging root, test repository, and model alias.
2. Provision the dedicated non-root identity and child-owned directories; verify no privileged groups and verify only ownership/mode of the model auth file.
3. Clone a disposable staging repository outside production projects. Confirm `origin` matches the configured coding repository.
4. Create parent-owned `coding.workspace_root` and `coding.staging_root` with group/other access denied.
5. Add a **staging-only** `dispatch.coding` block with `enabled: true`, one project `coding` override, one trusted test profile, and one allowed model alias.
6. Run `agent-handoff config validate --json`; confirm production projects remain read-only and coding is enabled only for the staging project.
7. Submit one staging coding Issue with `task: coding` and a small reviewed change.
8. Verify the child UID/GID, empty supplementary groups, dedicated HOME/XDG, no parent secrets, and a fresh clean workspace at the recorded base SHA.
9. Verify trusted tests run non-root with bounded timeout/output and that a forced failure blocks commit/PR while still delivering an honest report.
10. Verify one branch, one commit, and exactly one Draft PR; verify the base branch is unchanged and no force-push or merge occurred.
11. Verify the verified Drive report, receipt, Issue comment, and Telegram notification.
12. Restart the dispatcher and replay a poll; verify no duplicate agent run, branch, PR, or Issue comment.
13. Leave staging isolated or revert the staging config. Do not enable production coding until independent review and owner approval.

A produced test suite or a local fake run is not live acceptance. Mark unperformed steps `Not verified`.

### C1 versus C2

C1 provides configuration, identity, environment, workspace, and process-lifecycle safety. C2 adds the disposable coding workspace, trusted tests, diff validation, controlled commit/branch publication, Draft PR automation, durable coding stages, and recovery. C2 does not add OS account creation, provider-key creation, workspace provisioning, deployment, merge automation, or production enablement.
