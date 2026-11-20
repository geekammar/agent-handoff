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
