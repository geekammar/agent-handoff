from __future__ import annotations

import json
import shutil
from typing import Any

from ..util import run_command, sanitize_error


class GithubError(RuntimeError):
    pass


def _pages(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            flattened: list[dict[str, Any]] = []
            for item in value:
                flattened.extend(entry for entry in item if isinstance(entry, dict))
            return flattened
        return [item for item in value if isinstance(item, dict)]
    return []


class GithubTransport:
    def __init__(self, dispatch: dict[str, Any]):
        self.repository = (dispatch.get("repository") or "").strip()
        self.command = ((dispatch.get("github") or {}).get("command") or "gh").strip()
        self.allowed_users = {item.strip().lower() for item in (dispatch.get("allowed_users") or [])}
        self.require_private = bool((dispatch.get("github") or {}).get("require_private", True))
        self.current_user: str | None = None

    def _run(self, args: list[str], timeout: float = 60.0) -> str:
        binary = shutil.which(self.command) or (self.command if "/" in self.command else None)
        if not binary:
            raise GithubError(f"GitHub CLI is not installed or not on PATH: {self.command}")
        result = run_command([binary, *args], timeout=timeout)
        if result.returncode != 0:
            message = sanitize_error(result.stderr or result.stdout or f"gh exited {result.returncode}")
            raise GithubError(message)
        return result.stdout

    def _api(self, endpoint: str, *, paginate: bool = False, timeout: float = 60.0) -> Any:
        args = ["api"]
        if paginate:
            args.extend(["--paginate", "--slurp"])
        args.append(endpoint)
        text = self._run(args, timeout=timeout)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise GithubError(f"GitHub returned invalid JSON for {endpoint}: {exc}") from exc

    def verify_repository(self) -> dict[str, Any]:
        repo = self._api(f"repos/{self.repository}")
        if not isinstance(repo, dict):
            raise GithubError("GitHub repository metadata is not an object")
        full_name = str(repo.get("full_name") or "")
        if full_name.lower() != self.repository.lower():
            raise GithubError(f"configured repository does not match GitHub ({self.repository} != {full_name})")
        if self.require_private and not repo.get("private"):
            raise GithubError("the delivery repository must be private")
        if not repo.get("has_issues", True):
            raise GithubError("the delivery repository must have Issues enabled")
        user = self._api("user")
        login = str((user or {}).get("login") or "")
        if not login:
            raise GithubError("GitHub CLI is not authenticated as a user")
        self.current_user = login
        return {"repository": full_name, "private": bool(repo.get("private")), "user": login}

    def verify_inbox(self) -> dict[str, Any]:
        repo = self._api(f"repos/{self.repository}")
        if not isinstance(repo, dict):
            raise GithubError("GitHub repository metadata is not an object")
        full_name = str(repo.get("full_name") or "")
        if full_name.lower() != self.repository.lower():
            raise GithubError(f"configured repository does not match GitHub ({self.repository} != {full_name})")
        if self.require_private and not repo.get("private"):
            raise GithubError("the dispatch repository must be private")
        if not repo.get("has_issues", True):
            raise GithubError("the dispatch repository must have Issues enabled")
        user = self._api("user")
        login = str((user or {}).get("login") or "")
        if not login:
            raise GithubError("GitHub CLI is not authenticated as a user")
        if login.lower() not in self.allowed_users:
            raise GithubError("the authenticated GitHub user is not listed in dispatch.allowed_users")
        self.current_user = login
        return {"repository": full_name, "private": bool(repo.get("private")), "user": login}

    def list_open_issues(self) -> list[dict[str, Any]]:
        data = self._api(
            f"repos/{self.repository}/issues?state=open&sort=created&direction=asc&per_page=100",
            paginate=True,
            timeout=90,
        )
        return [issue for issue in _pages(data) if "pull_request" not in issue]

    def get_issue(self, number: int) -> dict[str, Any]:
        data = self._api(f"repos/{self.repository}/issues/{int(number)}")
        if not isinstance(data, dict):
            raise GithubError(f"issue #{number} metadata is not an object")
        return data

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        data = self._api(
            f"repos/{self.repository}/issues/{int(number)}/comments?per_page=100",
            paginate=True,
            timeout=60,
        )
        return _pages(data)

    def find_comment(self, number: int, marker: str) -> dict[str, Any] | None:
        for comment in self.list_comments(number):
            if marker in str(comment.get("body") or ""):
                return comment
        return None

    def comment(self, number: int, body: str) -> dict[str, Any]:
        text = self._run(
            ["issue", "comment", str(int(number)), "-R", self.repository, "--body", body],
            timeout=60,
        )
        return {"url": text.strip() or None}

    def issue_author_allowed(self, issue: dict[str, Any]) -> bool:
        login = str(((issue.get("user") or {}).get("login")) or "").strip().lower()
        return bool(login and login in self.allowed_users)
