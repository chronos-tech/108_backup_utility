"""Polite HTTP client: session reuse, throttling, retries, safe redirects, robots.txt.

Deliberately NOT implemented: proxy/IP rotation, CAPTCHA solving, header spoofing
or any other evasion. 403/429 are handled conservatively (see ``FetchResult.blocked``).

Nothing in this module logs URLs, cookies, headers or bodies.
"""

from __future__ import annotations

import email.utils
import http.cookiejar
import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from .config import Config
from .utils import same_site

log = logging.getLogger(__name__)

PREMIUM_MARKER = b"/2014/11/login-VIP.jpg"
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
MAX_RETRY_AFTER = 120.0   # a longer Retry-After means "stop", not "wait"
MIN_HTML_BYTES = 200
_REDIRECTS = (301, 302, 303, 307, 308)


@dataclass
class FetchResult:
    status: int | None = None
    body: bytes = b""
    content_type: str = ""
    error: str | None = None   # short code, never contains URL data
    blocked: bool = False      # 403 / 401 / 429 / over-long Retry-After

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200


def has_premium_marker(body: bytes) -> bool:
    return PREMIUM_MARKER in body


def validate_html(result: FetchResult) -> str | None:
    """Return an error code if the response is not a plausible HTML page."""
    if not result.ok:
        return result.error or "bad_status"
    ctype = result.content_type.lower()
    if ctype and "html" not in ctype:
        return "not_html"
    if len(result.body) < MIN_HTML_BYTES:
        return "too_short"
    head = result.body[:4096].lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        return "not_html"
    return None


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Throttle:
    """Global (cross-thread) spacing: each request starts a random delay after the last."""

    def __init__(
        self, delay_min: float, delay_max: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.delay_min, self.delay_max = delay_min, delay_max
        self._sleep, self._mono, self._rand = sleep, monotonic, rand
        self._lock = threading.Lock()
        self._next = 0.0

    def raise_minimum(self, seconds: float) -> None:
        with self._lock:
            self.delay_min = max(self.delay_min, seconds)
            self.delay_max = max(self.delay_max, self.delay_min)

    def wait(self) -> None:
        with self._lock:
            now = self._mono()
            start = max(now, self._next)
            self._next = start + self._rand(self.delay_min, self.delay_max)
        if start > now:
            self._sleep(start - now)


class HttpClient:
    def __init__(
        self, cfg: Config,
        sleep: Callable[[float], None] = time.sleep,
        throttle: Throttle | None = None,
    ) -> None:
        self._cfg = cfg
        self._sleep = sleep
        self.throttle = throttle or Throttle(cfg.delay_min, cfg.delay_max, sleep=sleep)
        self._base_host = urlsplit(cfg.base_url).hostname
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": self._cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
            })
            # Cookies are attached explicitly per request; never stored from responses.
            session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            self._local.session = session
        return session

    # ------------------------------------------------------------------
    def get(self, url: str, *, use_cookie: bool = False,
            max_bytes: int = MAX_BODY_BYTES) -> FetchResult:
        """GET with up to MAX_RETRIES retries and exponential backoff."""
        last = FetchResult(error="unknown")
        for attempt in range(MAX_RETRIES + 1):
            result, retry_after, retryable = self._attempt(url, use_cookie, max_bytes)
            if not retryable:
                return result
            last = result
            if attempt < MAX_RETRIES:
                backoff = min(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1), BACKOFF_CAP)
                self._sleep(max(backoff, retry_after or 0.0))
        if last.status == 429:
            last.blocked = True
        return last

    def _attempt(self, url: str, use_cookie: bool, max_bytes: int):
        headers: dict[str, str] = {}
        if use_cookie and self._cfg.cookie:
            headers["Cookie"] = self._cfg.cookie
        session = self._session()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            self.throttle.wait()
            try:
                resp = session.get(
                    current, headers=headers, timeout=self._cfg.request_timeout,
                    allow_redirects=False, stream=True,
                )
            except requests.Timeout:
                return FetchResult(error="timeout"), None, True
            except requests.RequestException:
                return FetchResult(error="connection"), None, True
            try:
                status = resp.status_code
                if status in _REDIRECTS:
                    target = urljoin(current, resp.headers.get("Location", ""))
                    parts = urlsplit(target)
                    if parts.scheme not in ("http", "https") or not same_site(
                        parts.hostname, self._base_host
                    ):
                        return FetchResult(status=status, error="offsite_redirect"), None, False
                    if urlsplit(current).scheme == "https" and parts.scheme == "http":
                        return FetchResult(status=status, error="downgrade_redirect"), None, False
                    if "wp-login.php" in parts.path:
                        return FetchResult(status=status, error="login_redirect"), None, False
                    current = target
                    continue
                if status == 200:
                    return self._read_ok(resp, max_bytes), None, False
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                    return FetchResult(status=status, error=f"http_{status}", blocked=True), None, False
                if status in (401, 403):
                    return FetchResult(status=status, error=f"http_{status}", blocked=True), None, False
                if status == 429 or 500 <= status <= 599:
                    return FetchResult(status=status, error=f"http_{status}"), retry_after, True
                return FetchResult(status=status, error=f"http_{status}"), None, False
            finally:
                resp.close()
        return FetchResult(error="too_many_redirects"), None, False

    @staticmethod
    def _read_ok(resp: requests.Response, max_bytes: int) -> FetchResult:
        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(65536):
                total += len(chunk)
                if total > max_bytes:
                    return FetchResult(status=200, error="too_large")
                chunks.append(chunk)
        except requests.RequestException:
            return FetchResult(status=200, error="connection")
        return FetchResult(
            status=200, body=b"".join(chunks),
            content_type=resp.headers.get("Content-Type", ""),
        )


class RobotsPolicy:
    """robots.txt for the configured site, fetched once, thread-safe.

    4xx -> allow all (RFC 9309). Unreachable/5xx -> disallow all this run (conservative).
    """

    def __init__(self, client: HttpClient, base_url: str, user_agent: str) -> None:
        self._client, self._base, self._ua = client, base_url, user_agent
        self._lock = threading.Lock()
        self._parser: urllib.robotparser.RobotFileParser | None = None
        self._allow_all = False
        self._disallow_all = False

    def _load(self) -> None:
        with self._lock:
            if self._parser is not None or self._allow_all or self._disallow_all:
                return
            parts = urlsplit(self._base)
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            res = self._client.get(robots_url)
            if res.ok:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(res.body.decode("utf-8", "replace").splitlines())
                self._parser = parser
                delay = parser.crawl_delay(self._ua)
                if delay:
                    self._client.throttle.raise_minimum(float(delay))
            elif res.status is not None and 400 <= res.status < 500:
                self._allow_all = True
            else:
                log.warning("robots.txt unreachable; skipping page fetches this run.")
                self._disallow_all = True

    def allowed(self, url: str) -> bool:
        self._load()
        if self._disallow_all:
            return False
        if self._allow_all or self._parser is None:
            return True
        return self._parser.can_fetch(self._ua, url)
