"""Metadata storage: ``data/cache.json``, ``data/premium.json`` and ``data/denied.json``.

All files are plain JSON objects ``{ "<AES token>": {record}, ... }``.
Plaintext URLs exist only in memory (``Registry._urls`` / ``Registry._index``).

Safety properties
-----------------
* Missing files, invalid JSON, duplicate JSON keys, malformed records, an
  undecryptable key, a SHA that does not match its URL, or two different tokens
  for the same URL are all fatal ``StorageError``/``CryptoError`` - nothing is
  overwritten.
* Writes are atomic (temp file + ``os.replace``) and skipped when unchanged.
* ``verify_append_only`` lets ``refresh`` prove nothing was removed or altered.
* ``file_lock`` serializes concurrent local runs (CI uses concurrency groups).
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from . import crypto
from .utils import atomic_write_bytes, normalize_url, utc_now_iso

try:  # POSIX only; CI runs on Linux. Elsewhere the lock is a no-op.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CACHE_PATH = "data/cache.json"
PREMIUM_PATH = "data/premium.json"
DENIED_PATH = "data/denied.json"
LOCK_PATH = "data/.lock"
VALID_TYPES = ("post", "page")


class StorageError(Exception):
    """Metadata is missing, malformed or inconsistent. Never includes URLs."""


# ---------------------------------------------------------------- JSON I/O

def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise StorageError("duplicate key in JSON object")
        out[key] = value
    return out


def read_json_object(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise StorageError(
            f"{path.name} is missing; restore it from git (an empty file is '{{}}')"
        ) from exc
    except OSError as exc:
        raise StorageError(f"cannot read {path.name}") from exc
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except StorageError:
        raise StorageError(f"{path.name} contains duplicate keys") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise StorageError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise StorageError(f"{path.name} must contain a JSON object")
    return data


def serialize(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


@contextlib.contextmanager
def file_lock(root: Path) -> Iterator[None]:
    """Exclusive advisory lock so two local runs cannot interleave writes."""
    lock_file = Path(root) / LOCK_PATH
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StorageError("another run holds the data lock") from exc
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


# ------------------------------------------------------------- validation

def _validate_record(name: str, rec: Any, url: str, *, premium: bool) -> None:
    if not isinstance(rec, dict):
        raise StorageError(f"{name}: record is not an object")
    status, sha, file_, ptype = (rec.get(k) for k in ("status", "sha", "file", "type"))
    if not isinstance(status, bool):
        raise StorageError(f"{name}: 'status' must be a boolean")
    if not isinstance(sha, str) or not crypto.SHA_RE.match(sha):
        raise StorageError(f"{name}: 'sha' must be a lowercase SHA-256 hex digest")
    if sha != crypto.url_sha256(url):
        raise StorageError(f"{name}: 'sha' does not match its encrypted URL")
    if ptype not in VALID_TYPES:
        raise StorageError(f"{name}: 'type' must be one of {VALID_TYPES}")
    expected = crypto.cache_filename(sha)
    if premium and not status:
        if file_ not in (None, expected):
            raise StorageError(f"{name}: unexpected 'file' value")
    elif file_ != expected:
        raise StorageError(f"{name}: 'file' must be {expected!r}")
    http_status = rec.get("http_status")
    if http_status is not None and (
        not isinstance(http_status, int) or isinstance(http_status, bool)
    ):
        raise StorageError(f"{name}: 'http_status' must be an integer or null")


def verify_append_only(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]], name: str
) -> None:
    """Raise unless every record in ``before`` is present and unchanged in ``after``."""
    for token, rec in before.items():
        if token not in after:
            raise StorageError(f"{name}: an existing record would be removed")
        if after[token] != rec:
            raise StorageError(f"{name}: an existing record would be modified")


# ---------------------------------------------------------------- Registry

class Registry:
    """All metadata files plus an in-memory plaintext index."""

    def __init__(
        self,
        root: Path,
        key: bytes,
        cache: dict[str, dict[str, Any]],
        premium: dict[str, dict[str, Any]],
        denied: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.root = Path(root)
        self._key = key
        self.cache = cache
        self.premium = premium
        self.denied = denied if denied is not None else {}
        self._urls: dict[str, str] = {}      # token -> normalized URL (memory only)
        self._index: dict[str, str] = {}     # normalized URL -> token
        self._build_index()

    def __repr__(self) -> str:  # never reveal contents
        return (f"Registry(cache={len(self.cache)}, premium={len(self.premium)}, "
                f"denied={len(self.denied)})")

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, root: Path, key: bytes) -> "Registry":
        root = Path(root)
        cache = read_json_object(root / CACHE_PATH)
        premium = read_json_object(root / PREMIUM_PATH)
        denied = read_json_object(root / DENIED_PATH)
        return cls(root, key, cache, premium, denied)

    def _build_index(self) -> None:
        # Recover from an interrupted cache->premium move: same token in both
        # files means the premium copy is authoritative.
        for token in [t for t in self.cache if t in self.premium]:
            log.warning("Recovered an interrupted premium move; dropping stale cache record.")
            del self.cache[token]
        # Same for an interrupted premium->denied move: the denied copy is authoritative.
        for token in [t for t in self.premium if t in self.denied]:
            log.warning("Recovered an interrupted denied move; dropping stale premium record.")
            del self.premium[token]

        for name, section, is_premium in (
            ("cache.json", self.cache, False),
            ("premium.json", self.premium, True),
            ("denied.json", self.denied, True),
        ):
            for token, rec in section.items():
                url = crypto.decrypt_url(self._key, token)  # CryptoError -> stop
                try:
                    normalized = normalize_url(url)
                except ValueError as exc:
                    raise StorageError(f"{name}: an encrypted URL is not a valid URL") from exc
                if normalized != url:
                    raise StorageError(f"{name}: an encrypted URL is not in normalized form")
                _validate_record(name, rec, url, premium=is_premium)
                if url in self._index:
                    raise StorageError(
                        "the same URL is registered under two different identifiers"
                    )
                self._urls[token] = url
                self._index[url] = token

    # -- queries ----------------------------------------------------------
    def has_url(self, url: str) -> bool:
        return url in self._index

    def url_for(self, token: str) -> str:
        return self._urls[token]

    def cache_file_exists(self, rec: dict[str, Any]) -> bool:
        file_ = rec.get("file")
        return bool(file_) and (self.root / file_).is_file()

    def snapshot(self) -> tuple[dict, dict]:
        return copy.deepcopy(self.cache), copy.deepcopy(self.premium)

    # -- mutations --------------------------------------------------------
    def add_new(self, url: str, page_type: str, now: str | None = None) -> bool:
        """Register a never-seen URL (fresh random nonce). Returns False if known."""
        if page_type not in VALID_TYPES:
            raise StorageError("invalid page type")
        if url in self._index:
            return False
        token = crypto.encrypt_url(self._key, url)
        while token in self.cache or token in self.premium or token in self.denied:  # astronomically unlikely
            token = crypto.encrypt_url(self._key, url)
        sha = crypto.url_sha256(url)
        self.cache[token] = {
            "status": False,
            "sha": sha,
            "file": crypto.cache_filename(sha),
            "type": page_type,
            "registered_at": now or utc_now_iso(),
            "fetched_at": None,
            "http_status": None,
        }
        self._urls[token] = url
        self._index[url] = token
        return True

    def move_to_premium(self, token: str, http_status: int | None, now: str) -> dict[str, Any]:
        """Move a pending cache record into premium.json (same token, same sha)."""
        old = self.cache.pop(token)
        rec = {
            "status": False,
            "sha": old["sha"],
            "file": None,
            "type": old["type"],
            "registered_at": old.get("registered_at"),
            "detected_at": now,
            "last_checked_at": now,
            "http_status": http_status,
        }
        self.premium[token] = rec
        return rec

    def move_to_denied(self, token: str, http_status: int | None, now: str) -> dict[str, Any]:
        """Move a premium record the session cannot access into denied.json."""
        old = self.premium.pop(token)
        rec = {
            "status": False,
            "sha": old["sha"],
            "file": None,
            "type": old["type"],
            "registered_at": old.get("registered_at"),
            "detected_at": old.get("detected_at"),
            "denied_at": now,
            "last_checked_at": now,
            "http_status": http_status,
        }
        self.denied[token] = rec
        return rec

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        """Write denied, then premium, then cache, so an interruption never loses a URL."""
        for rel, data in (
            (DENIED_PATH, self.denied),
            (PREMIUM_PATH, self.premium),
            (CACHE_PATH, self.cache),
        ):
            path = self.root / rel
            payload = serialize(data)
            try:
                if path.read_bytes() == payload:
                    continue
            except FileNotFoundError:
                pass
            atomic_write_bytes(path, payload)