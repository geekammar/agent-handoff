# Acceptance

## Baseline before repository refactor

Date: 2026-11-16

The existing v1.0 installation passed its full acceptance suite before any source changes:

- 26 passed
- 0 failed
- 0 not verified
- Real Google Drive: verified
- Real OpenCode and AGY sessions: captured
- Concurrent sessions, task/run attribution, retry recovery, report indexing, Drive isolation, duplicate suppression, and CLI compatibility: passed

The generated machine-readable result is `tests/acceptance-results.json` in the runtime home and is intentionally ignored by Git because it contains live run metadata.

## Automated CI suite

The repository CI runs unit, integration, regression, and installation tests without Google Drive credentials:

- `tests/test_unit.py`: redaction, report sections, config resolution, AGY outcome and transcript parsing.
- `tests/test_integration.py`: CLI compatibility, concurrent sessions, task/run attribution, upload retry, index generation, duplicate-upload suppression, secret redaction, AGY hook capture and deduplication, OpenCode plugin contract.
- `tests/test_install.py`: idempotent installation, runtime preservation, upgrade snapshot, and rollback.
- `tests/test_installer_safety.py`: Git and non-Git source selection, symlink containment, ownership manifests, coherent rollback, legacy snapshot refusal, external integration preservation, and failure recovery.

Command:

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Live acceptance suite

`tests/acceptance.py` performs synthetic checks against `tests/fake-rclone` and read-only live checks against the configured Google Drive hierarchy. Live checks are skipped when OAuth is unavailable.

Live-specific values are not stored in Git. The script reads folder IDs from the runtime configuration and optional `tests/live-config.json`:

```bash
cp tests/live-config.example.json tests/live-config.json
# set chat_folder_id, muse_folder_id, and git_project if isolation checks are required
python3 tests/acceptance.py
```

The suite writes `tests/acceptance-results.json` (ignored by Git).

## Recorded post-refactor result

Date: 2026-11-16

The refactored code was installed into the existing runtime home with a pre-upgrade snapshot. Results:

- Live acceptance suite: 26 passed, 0 failed, 0 not verified.
- Automated unit/integration/regression/install suite: 18 passed.
- `doctor`: all checks PASS, including Google Drive access and both configured project folder IDs.
- Runtime configuration hash unchanged.
- rclone configuration hash unchanged.
- Local report files preserved: 31 before and after.
- Upload queue preserved: 0 before and after.
- Real OpenCode and AGY production runs remained upload-verified with matching remote MD5 values.
- No duplicate root or project folder was created.
- Other Drive subtrees remained free of Agent Handoff artifacts.

The generated result file is `$AGENT_HANDOFF_HOME/tests/acceptance-results.json` and is ignored by Git. This document intentionally does not embed live task IDs or folder IDs.

## v1.1 MVP automated acceptance

Date: 2026-11-19

Command:

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Deterministic result: **42 tests passed, 0 failed**. The dispatch suite uses fake GitHub, fake OpenCode, and fake rclone; it does not require provider, GitHub, or Drive credentials.

Covered dispatcher acceptance:

- valid request executes exactly once and produces a verified direct Drive URL;
- duplicate polling does not repeat execution or duplicate the issue comment;
- unauthorized authors, malformed requests, unknown projects, unknown models, unsupported agents, and SAME SESSION are rejected without execution;
- ProjectA and ProjectB execute in isolated workspaces with correct `cwd` and `PWD`;
- a dispatch workspace that does not exactly match its core project path is rejected by config validation;
- execution output is linked to the correct Agent Handoff task/run and session/model metadata;
- upload failure retries upload without restarting OpenCode;
- GitHub publication failure retries publication without re-upload or execution and without duplicate links;
- a restart after claim does not execute the model again and marks the record `NEEDS_ATTENTION`;
- a restart after verified upload publishes without another execution;
- historical-style stale remote `COMPLETE.json` is reconciled with a verified receipt and direct URL without executing an agent;
- core capture/upload still works with dispatch disabled;
- install, upgrade, and rollback preserve runtime config, reports/data, credentials-side files, and dispatch ledger state;
- example configurations contain only generic users, paths, models, and projects.

Live checks are reported separately in the implementation report. A live check is `Not verified` unless the exact host command and result are recorded.

## v1.1 real end-to-end acceptance

Date: 2026-11-19

A real private GitHub Issue was dispatched through the v1.1 dispatcher using real `gh`, real OpenCode, and the real Google Drive hierarchy. The execution workspace was a disposable sandbox, not Rabet or Yakfi.

Post-fix result:

- exactly one OpenCode NEW SESSION;
- correct sandbox marker, model, session, task, and run recorded;
- REPORT.md uploaded and verified by MD5;
- `UPLOAD-RECEIPT.json` created and verified;
- remote `COMPLETE.json` reconciled to `upload.verified=true` with a direct Drive URL;
- direct verified report URL posted to the originating Issue;
- a second dispatcher process produced zero additional executions, zero duplicate report comments, and identical task/run/session identity;
- the read-only permission probe denied a requested file write in a disposable sandbox.

The review found one report-truthfulness defect: when a workspace had no Git remote, the dispatcher used the dispatch Issue repository as the code repository. That fallback was removed, a regression test was added, and the real acceptance was repeated successfully. Exact live identifiers are recorded in the release-review Agent Handoff report rather than in this source document.

## v1.2 P0 deterministic acceptance

Date: 2026-11-20

Command:

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Deterministic result: **56 tests passed, 0 failed**. This run used fake GitHub, fake OpenCode, and fake rclone; it does not prove a live provider, GitHub, or Drive path.

Additional P0 coverage:

- long OpenCode execution emits an `executing` heartbeat without prompt text or workspace paths;
- execution timeout and dispatcher SIGTERM terminate the OpenCode child process;
- slow rclone folder listing emits a `waiting` heartbeat without local paths;
- `--watch --json` emits one valid compact JSON object per line;
- `dispatch deliver --json --progress` keeps JSON on stdout and progress on stderr;
- upload backoff is honored without `--force`;
- `delivery.max_attempts` exhaustion stops automatic publication retries and is reported as `attention`;
- reused delivery reconciliation preserves verified-at metadata without another upload.

Live acceptance remains a separate, operator-controlled step. It was not performed as part of this change.

## C1 secure coding foundation deterministic acceptance

Date: 2026-11-21

Command:

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
```

Deterministic result: **139 tests passed, 0 failed**. The focused C1 module ran **37 tests, 0 failed**. This run used fake GitHub, fake OpenCode, and fake rclone. It does not prove a live dedicated Unix account, provider credential, or production coding path.

C1 focused coverage (`tests/test_c1_isolation.py`):

- legacy config and Rabet/Yakfi-style read-only inheritance remain unchanged;
- mixed configuration: Rabet and Yakfi stay legacy read-only while AgentHandoff opts into isolated `workspace_write`;
- legacy global `isolation.enabled: true` still isolates every project, and an explicit per-project `false` opt-out is preserved for read-only projects;
- valid per-project write policy and explicit authorization checks;
- write-capable policy without effective isolation is rejected instead of running as root;
- malformed project isolation overrides and missing dedicated identity fail closed;
- Issue metadata cannot change policy, UID/GID, isolation, workspace, or model allowlist;
- child environment excludes parent Telegram/GitHub/rclone/runtime secrets while the parent keeps them;
- dedicated HOME/XDG/TMPDIR and child-owned model auth are required; root auth is never inherited;
- canonical path, symlink escape, protected path, denied project, writable ancestor, unsafe Git metadata, and dirty-tree refusal without resetting work;
- trusted versus untrusted OpenCode executable paths, including child-owned, group/world-writable, and child-HOME/workspace/cache/TMPDIR locations;
- timeout, progress heartbeat, and no-orphan process cleanup;
- real Python child UID/GID drop with empty supplementary groups and a denied root-only secret read when the test runs as root with an unprivileged account available;
- real mixed-project dispatcher run under `nobody`: Rabet/Yakfi execute legacy read-only as root while AgentHandoff executes isolated `workspace_write` with the dedicated UID/GID, empty supplementary groups, dedicated HOME, no parent GitHub token, and effective isolation persisted in the ledger. These OS-level proofs were **skipped** on non-root CI runners because dropping to another UID requires root.

Live staging acceptance of an actual isolated OpenCode coding task was **not performed** in this change. It requires a separately approved Unix account, provider credential, and disposable workspace. Production Rabet/Yakfi remain `read_only`, production isolation remains disabled, and the production service was not restarted.

## C2 focused security, artifact integrity, and recovery repair

Date: 2026-11-21

Commands:

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
PYTHONPATH=lib python3 -m unittest tests.test_c2_coding -v
git diff --check
```

Deterministic result: **188 tests passed, 0 failed**. The focused C2 module ran **49 tests, 0 failed**. New coverage includes artifact digest/size/mode changes after validation, symlink substitution, manifest/index/committed-tree verification, unexpected staged paths, hook/filter/fsmonitor non-execution, restricted file protocol, no force push, clean-workspace adoption, unpersisted-commit recreation, missing-staging remote-SHA recovery, PR body retry, orphan-process blocking, SIGTERM unwinding, bounded output/resource limits, and historical-ledger/status compatibility. These are deterministic local tests, not live C2 staging acceptance.

## C2 isolated coding workflow deterministic acceptance

Date: 2026-11-21

Commands:

```bash
python3 -m compileall -q lib setup tests
sh -n setup/install.sh setup/upgrade.sh setup/rollback.sh setup/gdrive-auth.sh
PYTHONPATH=lib python3 -m unittest discover -s tests -p "test_*.py" -v
PYTHONPATH=lib python3 -m unittest tests.test_c2_coding -v
git diff --check
```

Deterministic result: **169 tests passed, 0 failed** at the initial C2 review point; the focused C2 module ran **30 tests, 0 failed**. The focused repair run above supersedes these counts with 188/49. The suite uses fake GitHub, fake OpenCode, synthetic credentials, a local trusted Git remote, and fake rclone. It does not prove a live dedicated Unix account, live provider credential, real private GitHub repository, or production enablement.

C2 focused coverage (`tests/test_c2_coding.py`, mapped to T01-T40 in test names):

- legacy read-only request contract and existing C1/dispatcher/notification tests remain unchanged;
- coding is disabled by default and requires global + project opt-in and effective non-root isolation;
- unauthorized authors are rejected without execution;
- Issue metadata cannot add commands, profiles, tests, identity, paths, or unknown task types;
- valid coding requests require a trusted model alias;
- dedicated non-root test identity and no parent secrets in test/child environments;
- unique clean workspaces at the verified base commit; dirty/existing and symlink workspaces are refused;
- concurrent task workspaces are isolated;
- realistic simulated code changes, trusted argv test execution, timeout/failure recording, and root-test refusal;
- protected paths, allowlists, secret scanning with redaction, and empty-change refusal;
- branch collision refusal, no default-branch push, commit SHA recording, one Draft PR, and no force-push;
- lost push and PR responses recover without duplicates;
- restart after publication and upload failure do not rerun OpenCode or duplicate the PR/comment;
- report, receipt, Issue comment, and Telegram payload include the verified report URL and Draft PR when present;
- JSON progress and existing notification behavior remain covered by the full suite;
- config validation does not mutate files, unrelated historical queue items are not processed, and Rabet/Yakfi-style projects remain legacy read-only.

Not performed: a live C2 staging coding Issue, real dedicated Unix UID/GID drop for the C2 test runner, real provider/model execution, real private-repository push, real Draft PR creation, and Telegram delivery. These require explicit owner provisioning and are listed in the C2 staging acceptance procedure. Production coding remains disabled and the production service was not restarted.
