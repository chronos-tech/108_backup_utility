"""Environment-only configuration with up-front validation.

Nothing here is ever logged: ``Config.__repr__`` hides the AES key and cookie.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "wp-sitemap-cache/1.0 (personal cache; respects robots.txt)"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


def _opt(env: Mapping[str, str], name: str) -> str | None:
    """Return a stripped env value, or None when unset/blank.

    GitHub passes unset Variables/Secrets as empty strings, so blank == unset.
    """
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_bool(raw: str | None, name: str, errors: list[str]) -> bool:
    if raw is None:
        return False
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    errors.append(f"{name} must be true or false")
    return False


def _parse_number(
    raw: str | None, name: str, default: float, lo: float, hi: float,
    errors: list[str], *, integer: bool = False,
) -> float:
    if raw is None:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        errors.append(f"{name} must be a number")
        return default
    if not (lo <= value <= hi):
        errors.append(f"{name} must be between {lo} and {hi}")
        return default
    return value


def _normalize_base_url(raw: str, errors: list[str]) -> str:
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        errors.append("BASE_URL is not a valid URL")
        return raw
    if parts.scheme not in ("http", "https") or not host:
        errors.append("BASE_URL must start with http:// or https:// and include a host")
    elif parts.username or parts.password:
        errors.append("BASE_URL must not contain credentials")
    elif parts.query or parts.fragment:
        errors.append("BASE_URL must not contain a query string or fragment")
    return raw.rstrip("/")


@dataclass(frozen=True)
class Config:
    base_url: str
    aes_key: str = field(repr=False)
    age_recipient: str | None
    cookie: str | None = field(default=None, repr=False)
    fetch_premium: bool = False
    max_workers: int = 2
    request_timeout: float = 30.0
    delay_min: float = 2.0
    delay_max: float = 5.0
    max_valid_pages: int = 0  # stop after N validly cached pages; 0 = download all
    max_premium_pages: int = 0  # premium mode: attempt at most N premium pages; 0 = all
    max_runtime_minutes: float = 0.0  # graceful stop after this long; 0 = no limit
    user_agent: str = DEFAULT_USER_AGENT

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_age_recipient: bool = True,
    ) -> "Config":
        """Build a validated Config. Collects every problem before raising."""
        env = os.environ if env is None else env
        errors: list[str] = []

        base_url = _opt(env, "BASE_URL")
        if base_url is None:
            errors.append("BASE_URL is required")
            base_url = ""
        else:
            base_url = _normalize_base_url(base_url, errors)

        aes_key = _opt(env, "AES_KEY")
        if aes_key is None:
            errors.append("AES_KEY is required")
            aes_key = ""

        age_recipient = _opt(env, "AGE_RECIPIENT")
        if age_recipient is None and require_age_recipient:
            errors.append("AGE_RECIPIENT is required")

        cookie = _opt(env, "COOKIE")
        if cookie is not None and any(c in cookie for c in "\r\n\x00"):
            errors.append("COOKIE must not contain line breaks or NUL characters")
            cookie = None

        fetch_premium = _parse_bool(_opt(env, "FETCH_PREMIUM"), "FETCH_PREMIUM", errors)
        max_workers = int(_parse_number(
            _opt(env, "MAX_WORKERS"), "MAX_WORKERS", 2, 1, 8, errors, integer=True))
        timeout = _parse_number(
            _opt(env, "REQUEST_TIMEOUT"), "REQUEST_TIMEOUT", 30.0, 1, 300, errors)
        delay_min = _parse_number(
            _opt(env, "REQUEST_DELAY_MIN"), "REQUEST_DELAY_MIN", 2.0, 0, 60, errors)
        delay_max = _parse_number(
            _opt(env, "REQUEST_DELAY_MAX"), "REQUEST_DELAY_MAX", 5.0, 0, 120, errors)
        if delay_min > delay_max:
            errors.append("REQUEST_DELAY_MIN must not exceed REQUEST_DELAY_MAX")
        max_valid = int(_parse_number(
            _opt(env, "MAX_VALID_PAGES"), "MAX_VALID_PAGES", 0, 0, 1_000_000,
            errors, integer=True))
        max_premium = int(_parse_number(
            _opt(env, "MAX_PREMIUM_PAGES"), "MAX_PREMIUM_PAGES", 0, 0, 1_000_000,
            errors, integer=True))
        max_runtime = _parse_number(
            _opt(env, "MAX_RUNTIME_MINUTES"), "MAX_RUNTIME_MINUTES", 0.0, 0, 10_000, errors)
        user_agent = _opt(env, "USER_AGENT") or DEFAULT_USER_AGENT
        if any(c in user_agent for c in "\r\n"):
            errors.append("USER_AGENT must not contain line breaks")

        if errors:
            raise ConfigError("Invalid configuration: " + "; ".join(errors))

        return cls(
            base_url=base_url,
            aes_key=aes_key,
            age_recipient=age_recipient,
            cookie=cookie,
            fetch_premium=fetch_premium,
            max_workers=max_workers,
            request_timeout=timeout,
            delay_min=delay_min,
            delay_max=delay_max,
            max_valid_pages=max_valid,
            max_premium_pages=max_premium,
            max_runtime_minutes=max_runtime,
            user_agent=user_agent,
        )