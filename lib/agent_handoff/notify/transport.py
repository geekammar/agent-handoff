from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from ..util import redact_text

DEFAULT_API_BASE = "https://api.telegram.org"
MAX_RESPONSE_BYTES = 65536


def _api_base() -> str:
    value = os.environ.get("AGENT_HANDOFF_TELEGRAM_API_BASE", "").strip() or DEFAULT_API_BASE
    return value.rstrip("/")


def _safe_error(value: Any, token: str) -> str:
    text = str(value or "Telegram request failed")
    if token:
        text = text.replace(token, "[REDACTED]")
    text, _count = redact_text(text)
    text = " ".join(text.split())
    if len(text) > 400:
        text = text[:399].rstrip() + "…"
    return text


def _retry_after(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return min(number, 3600)


def _failure(
    *,
    status: int,
    description: str,
    token: str,
    retryable: bool,
    retry_after: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "message_id": None,
        "status_code": status or None,
        "retryable": retryable,
        "retry_after": retry_after,
        "error": _safe_error(description, token),
    }


class TelegramTransport:
    def __init__(self, token: str, *, timeout_seconds: int = 10, api_base: str | None = None):
        self._token = token
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.api_base = (api_base or _api_base()).rstrip("/")

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_web_page_preview: bool = True,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_base}/bot{self._token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            try:
                raw = exc.read(MAX_RESPONSE_BYTES)
            except OSError:
                raw = b""
            return self._parse_response(status, raw, retryable=status == 429 or status >= 500)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return _failure(
                status=0,
                description=f"Telegram network failure: {reason}",
                token=self._token,
                retryable=True,
            )
        except Exception as exc:
            return _failure(
                status=0,
                description=f"Telegram request failed: {exc.__class__.__name__}",
                token=self._token,
                retryable=False,
            )
        return self._parse_response(status, raw, retryable=status == 429 or status >= 500)

    def _parse_response(self, status: int, raw: bytes, *, retryable: bool) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = {}
        description = str(data.get("description") or "").strip()
        if 200 <= status < 300 and data.get("ok") is True:
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            return {
                "ok": True,
                "message_id": result.get("message_id"),
                "status_code": status,
                "retryable": False,
                "retry_after": None,
                "error": None,
            }
        parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        retry_after = _retry_after(parameters.get("retry_after"))
        if not description:
            description = f"Telegram API returned HTTP {status or 'unknown'}"
            if data.get("ok") is False:
                description = f"Telegram rejected the message (HTTP {status or 'unknown'})"
        return _failure(
            status=status,
            description=description,
            token=self._token,
            retryable=retryable or bool(retry_after),
            retry_after=retry_after,
        )
