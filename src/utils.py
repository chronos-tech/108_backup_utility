"""Small shared helpers: logging, URL normalization, atomic writes, timestamps."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = lambda *args: datetime.now(timezone.utc).timetuple()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` used for dedup, hashing and encryption.

    Rules: scheme/host lowercased, IDNA host, default port dropped, fragment
    dropped, empty path becomes "/". Path, trailing slash and query are kept
    exactly as given because they can change which page the server returns.

    Raises ValueError for anything that is not a plain http(s) URL.
    """
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    url = url.strip()
    if not url or any(ch in url for ch in " \t\r\n"):
        raise ValueError("invalid URL")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc

    host = host.lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"

    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def same_site(host_a: str | None, host_b: str | None) -> bool:
    """True when two hostnames are the same site (a leading ``www.`` is ignored)."""
    if not host_a or not host_b:
        return False

    def _strip(host: str) -> str:
        host = host.lower().rstrip(".")
        return host[4:] if host.startswith("www.") else host

    return _strip(host_a) == _strip(host_b)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file in same dir + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
