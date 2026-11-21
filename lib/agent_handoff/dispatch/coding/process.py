from __future__ import annotations

import os
import signal
import time


def process_group_alive(pgid: int | None) -> bool:
    if not pgid or int(pgid) <= 0:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_process_group(pgid: int | None, *, grace_seconds: float = 5.0) -> bool:
    """Terminate a child process group and verify it is gone."""

    if not pgid or int(pgid) <= 0:
        return True
    pgid = int(pgid)
    if not process_group_alive(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            return True
        time.sleep(0.05)
    return not process_group_alive(pgid)
