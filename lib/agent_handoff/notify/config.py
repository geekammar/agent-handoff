from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_TOKEN_ENV = "AGENT_HANDOFF_TELEGRAM_TOKEN"
TOKEN_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHAT_ID_RE = re.compile(r"^(?:-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{10,}$")


class NotificationConfigError(ValueError):
    pass


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _add(errors: list[str], message: str) -> None:
    if message not in errors:
        errors.append(message)


def _int_value(value: Any, default: int, minimum: int, maximum: int, label: str, errors: list[str]) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        _add(errors, f"{label} must be an integer")
        return default
    if number < minimum or number > maximum:
        _add(errors, f"{label} must be between {minimum} and {maximum}")
        return default
    return number


def notification_token_env_names(config: dict[str, Any]) -> tuple[str, ...]:
    """Environment variable names that may hold notification credentials.

    The dispatcher reads the token from the parent environment, but coding
    agents launched by the dispatcher must never inherit it. The default name
    is always included so an accidentally exported token is still filtered
    when notifications are disabled or unconfigured.
    """

    names = [DEFAULT_TOKEN_ENV]
    raw = config.get("notifications")
    if isinstance(raw, dict):
        telegram = raw.get("telegram")
        if isinstance(telegram, dict):
            token_env = _text(telegram.get("token_env")) or DEFAULT_TOKEN_ENV
            if TOKEN_ENV_RE.fullmatch(token_env) and token_env not in names:
                names.append(token_env)
    return tuple(names)


def validate_notification_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate the optional notification section without contacting Telegram.

    The returned mapping may include the bot token read from the configured
    environment variable. Callers must never print it. Validation errors never
    contain the token value or any other environment value.
    """

    raw = config.get("notifications")
    errors: list[str] = []
    warnings: list[str] = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise NotificationConfigError("notifications configuration must be an object")

    telegram = raw.get("telegram")
    if telegram is None:
        telegram = {}
    if not isinstance(telegram, dict):
        raise NotificationConfigError("notifications.telegram configuration must be an object")

    enabled_raw = telegram.get("enabled", False)
    if not isinstance(enabled_raw, bool):
        _add(errors, "notifications.telegram.enabled must be true or false")
        enabled = False
    else:
        enabled = enabled_raw

    chat_raw = telegram.get("chat_id")
    if isinstance(chat_raw, int) and not isinstance(chat_raw, bool):
        chat_id = str(chat_raw)
    else:
        chat_id = _text(chat_raw)
        if chat_raw is not None and not isinstance(chat_raw, str):
            _add(errors, "notifications.telegram.chat_id must be a string chat id or @channelusername")
    if chat_id and not CHAT_ID_RE.fullmatch(chat_id):
        _add(
            errors,
            "notifications.telegram.chat_id must be a numeric Telegram chat id or an @channelusername",
        )
    if enabled and not chat_id:
        _add(errors, "notifications.telegram.chat_id is required when notifications.telegram.enabled is true")

    token_env_raw = telegram.get("token_env")
    token_env = _text(token_env_raw) or DEFAULT_TOKEN_ENV
    if token_env_raw is not None and not isinstance(token_env_raw, str):
        _add(errors, "notifications.telegram.token_env must be an environment variable name string")
    if not TOKEN_ENV_RE.fullmatch(token_env):
        _add(
            errors,
            "notifications.telegram.token_env must be an environment variable name "
            "(letters, digits, and underscores; not a token value)",
        )

    timeout_seconds = _int_value(
        telegram.get("timeout_seconds"), 10, 1, 120, "notifications.telegram.timeout_seconds", errors
    )
    retry_seconds = _int_value(
        telegram.get("retry_seconds"), 60, 5, 3600, "notifications.telegram.retry_seconds", errors
    )
    max_attempts = _int_value(
        telegram.get("max_attempts"), 5, 1, 20, "notifications.telegram.max_attempts", errors
    )
    delayed_after_attempts = _int_value(
        telegram.get("delayed_after_attempts"),
        3,
        1,
        100,
        "notifications.telegram.delayed_after_attempts",
        errors,
    )

    token = None
    if enabled and TOKEN_ENV_RE.fullmatch(token_env):
        token = _text(os.environ.get(token_env))
        if not token:
            _add(
                errors,
                f"notifications.telegram.token_env refers to {token_env}, which is not set; "
                "export the Telegram bot token in that environment variable",
            )
        elif not BOT_TOKEN_RE.fullmatch(token):
            _add(
                errors,
                f"the environment variable {token_env} does not look like a Telegram bot token "
                "(expected digits, a colon, then token characters)",
            )
            token = None

    resolved = {
        "enabled": enabled,
        "chat_id": chat_id or None,
        "token_env": token_env,
        "token": token,
        "timeout_seconds": timeout_seconds,
        "retry_seconds": retry_seconds,
        "max_attempts": max_attempts,
        "delayed_after_attempts": delayed_after_attempts,
    }
    return resolved, errors, warnings
