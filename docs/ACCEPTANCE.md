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
