#!/usr/bin/env python3
"""Fetch pending pages, age-encrypt them, update metadata.

Modes (``MAX_VALID_PAGES``):
  0 (default)  download everything that is eligible
  N > 0        stop after N validly cached pages (failures/premium-detections
               do not count), leaving the rest pending for a later run

Exit codes: 0 ok, 1 fatal (crypto/storage), 2 bad configuration,
3 stopped early because the site returned repeated 403/429.

Premium mode (``FETCH_PREMIUM`` true): only premium.json is processed (cache pages are
skipped). ``MAX_PREMIUM_PAGES`` N > 0 attempts at most N premium pages, oldest-checked
first; denied pages move to denied.json and count toward N.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from src import crypto, fetcher, storage
from src.config import Config, ConfigError
from src.fetcher import HttpClient, RobotsPolicy
from src.utils import atomic_write_bytes, setup_logging, utc_now_iso

log = logging.getLogger("update")
ROOT = Path(__file__).resolve().parent
CHECKPOINT_EVERY = 25
BLOCK_LIMIT = 5  # consecutive 403/429 before the run stops


@dataclass
class WorkItem:
    token: str
    url: str          # plaintext, memory only
    section: str      # "cache" | "premium"


@dataclass
class Outcome:
    item: WorkItem
    kind: str         # cached | premium_granted | premium_new | premium_denied |
                      # failed | blocked | robots | stopped
    http_status: int | None = None
    html: bytes | None = None


@dataclass
class UpdateSummary:
    skipped_cached: int = 0
    skipped_premium: int = 0
    fetched: int = 0
    premium_detected: int = 0
    premium_fetched: int = 0
    premium_denied: int = 0
    failed: int = 0
    robots_skipped: int = 0
    premium_left: int = 0     # premium.json records still pending after the run
    premium_total: int = 0
    denied_total: int = 0
    denied_shas: list[str] = field(default_factory=list)  # this run; sha only, never URLs
    deferred: int = 0       # eligible but not attempted (limit/stop)
    blocked_stop: bool = False


class RunState:
    """Thread-safe run control shared by all workers.

    * ``claim_valid`` enforces the MAX_VALID_PAGES limit exactly: a worker that
      would exceed it drops its result and the page simply stays pending.
    * ``note`` trips the circuit breaker after BLOCK_LIMIT consecutive 403/429.
    """

    def __init__(self, limit: int = 0, deadline: float | None = None) -> None:
        self.deadline = deadline          # time.monotonic() value, or None
        self.stop = threading.Event()
        self.blocked_stop = False
        self._limit = limit
        self._valid = 0
        self._blocks = 0
        self._lock = threading.Lock()

    def should_stop(self) -> bool:
        if not self.stop.is_set() and self.deadline is not None and time.monotonic() >= self.deadline:
            self.stop.set()
        return self.stop.is_set()

    def claim_valid(self) -> bool:
        with self._lock:
            if self._limit and self._valid >= self._limit:
                return False
            self._valid += 1
            if self._limit and self._valid >= self._limit:
                self.stop.set()
            return True

    def note(self, blocked: bool) -> None:
        with self._lock:
            self._blocks = self._blocks + 1 if blocked else 0
            if self._blocks >= BLOCK_LIMIT:
                self.blocked_stop = True
                self.stop.set()


# ------------------------------------------------------------ worker side

def _classify(res: fetcher.FetchResult) -> tuple[str, str | None]:
    """-> (blocked | login | premium | ok | error, error_code)."""
    if res.blocked:
        return "blocked", res.error
    if res.error == "login_redirect":
        return "login", res.error
    if res.ok and fetcher.has_premium_marker(res.body):
        return "premium", None
    err = fetcher.validate_html(res)
    return ("ok", None) if err is None else ("error", err)


def process_item(item: WorkItem, cfg: Config, client: HttpClient,
                 robots: RobotsPolicy, state: RunState) -> Outcome:
    if state.should_stop():
        return Outcome(item, "stopped")
    if not robots.allowed(item.url):
        return Outcome(item, "robots")

    can_auth = cfg.fetch_premium and bool(cfg.cookie)

    def finish_ok(kind: str, res: fetcher.FetchResult) -> Outcome:
        state.note(False)
        if not state.claim_valid():
            return Outcome(item, "stopped")      # limit reached: stays pending
        return Outcome(item, kind, res.status, res.body)

    def finish_bad(kind: str, res: fetcher.FetchResult) -> Outcome:
        state.note(kind == "blocked")
        return Outcome(item, kind, res.status)

    if item.section == "premium":
        res = client.get(item.url, use_cookie=True)   # only reached when can_auth
        kind, _ = _classify(res)
        if kind == "ok":
            return finish_ok("premium_granted", res)
        if kind in ("premium", "login"):
            state.note(False)
            return Outcome(item, "premium_denied", res.status)
        return finish_bad("blocked" if kind == "blocked" else "failed", res)

    # Unknown page: always fetched anonymously first so premium pages are detected.
    res = client.get(item.url)
    kind, _ = _classify(res)
    if kind == "ok":
        return finish_ok("cached", res)
    if kind == "premium":
        state.note(False)
        if not can_auth:
            return Outcome(item, "premium_new", res.status)
        if state.should_stop():
            return Outcome(item, "stopped")
        res2 = client.get(item.url, use_cookie=True)
        kind2, _ = _classify(res2)
        if kind2 == "ok":
            return finish_ok("premium_granted", res2)
        state.note(kind2 == "blocked")
        return Outcome(item, "premium_new", res.status)   # access denied: keep record
    return finish_bad("blocked" if kind == "blocked" else "failed", res)


# ------------------------------------------------------------- main thread

def _build_work(registry: storage.Registry, cfg: Config, summary: UpdateSummary) -> list[WorkItem]:
    items: list[tuple[str, str, str]] = []   # (last_checked, token, section)
    can_auth = cfg.fetch_premium and bool(cfg.cookie)
    if cfg.fetch_premium and not cfg.cookie:
        log.warning("FETCH_PREMIUM is true but COOKIE is not set; nothing to fetch.")

    for token, rec in registry.cache.items():
        if rec["status"] is True and registry.cache_file_exists(rec):
            summary.skipped_cached += 1
        else:
            items.append((rec.get("last_checked_at") or "", token, "cache"))

    for token, rec in registry.premium.items():
        done = rec["status"] is True and registry.cache_file_exists(rec)
        if done or not can_auth:
            summary.skipped_premium += 1
        else:
            items.append((rec.get("last_checked_at") or "", token, "premium"))

    items.sort()  # never-attempted first, then least recently checked
    if can_auth and cfg.max_premium_pages and len(items) > cfg.max_premium_pages:
        summary.deferred += len(items) - cfg.max_premium_pages
        items = items[:cfg.max_premium_pages]
    return [WorkItem(token, registry.url_for(token), section) for _, token, section in items]


def _write_cache_file(root: Path, rec: dict, html: bytes, recipient) -> None:
    encrypted = crypto.encrypt_html(html, recipient)       # CryptoError -> fatal
    atomic_write_bytes(root / crypto.cache_filename(rec["sha"]), encrypted)


def run_update(cfg: Config, root: Path, client: HttpClient | None = None,
               robots: RobotsPolicy | None = None) -> UpdateSummary:
    key, recipient = crypto.validate_crypto_config(cfg.aes_key, cfg.age_recipient)
    client = client or HttpClient(cfg)
    robots = robots or RobotsPolicy(client, cfg.base_url, cfg.user_agent)
    summary = UpdateSummary()
    deadline = (time.monotonic() + cfg.max_runtime_minutes * 60) if cfg.max_runtime_minutes else None
    state = RunState(cfg.max_valid_pages, deadline)

    with storage.file_lock(root):
        registry = storage.Registry.load(root, key)
        work = _build_work(registry, cfg, summary)
        applied = 0
        executor = ThreadPoolExecutor(max_workers=cfg.max_workers)
        try:
            futures = [executor.submit(process_item, w, cfg, client, robots, state) for w in work]
            for future in as_completed(futures):
                out = future.result()
                if out.kind == "stopped":
                    summary.deferred += 1
                    continue
                _apply(out, registry, root, recipient, summary, utc_now_iso())
                applied += 1
                if applied % CHECKPOINT_EVERY == 0:
                    registry.save()
        finally:
            state.stop.set()
            registry.save()   # progress is saved even on SIGTERM/SIGINT/errors
            executor.shutdown(wait=False, cancel_futures=True)
        summary.blocked_stop = state.blocked_stop

    summary.premium_total = len(registry.premium)
    summary.premium_left = sum(1 for r in registry.premium.values() if r.get("status") is not True)
    summary.denied_total = len(registry.denied)

    log.info("Skipped %d successfully cached records.", summary.skipped_cached)
    log.info("Fetched %d pages successfully.", summary.fetched)
    log.info("Detected %d premium pages.", summary.premium_detected)
    log.info("Fetched %d premium pages with access.", summary.premium_fetched)
    log.info("Premium access denied for %d pages.", summary.premium_denied)
    log.info("Failed to fetch %d pages.", summary.failed)
    if summary.robots_skipped:
        log.info("Skipped %d pages disallowed by robots.txt.", summary.robots_skipped)
    if summary.deferred:
        log.info("%d eligible pages left for a later run.", summary.deferred)
    if summary.blocked_stop:
        log.warning("Stopped early: site returned repeated 403/429 responses.")
    if cfg.fetch_premium:
        _report_premium(summary)
    return summary


def _report_premium(summary: UpdateSummary) -> None:
    """Premium run report: counts and sha256 identifiers only, never URLs."""
    lines = [
        f"Premium left: {summary.premium_left} of {summary.premium_total} pending in premium.json.",
        f"Premium denied this run: {len(summary.denied_shas)} "
        f"(denied.json total: {summary.denied_total}).",
    ]
    lines += [f"Denied: {sha}" for sha in summary.denied_shas]
    for line in lines:
        log.info("%s", line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("### Premium run\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")
        except OSError:
            log.warning("Could not write the step summary.")


def _apply(out: Outcome, registry: storage.Registry, root: Path, recipient,
           summary: UpdateSummary, now: str) -> None:
    item = out.item
    if out.kind == "robots":
        summary.robots_skipped += 1
        return

    if item.section == "cache":
        rec = registry.cache[item.token]
        if out.kind == "cached":
            _write_cache_file(root, rec, out.html, recipient)
            rec.update(status=True, fetched_at=now, last_checked_at=now,
                       http_status=out.http_status)
            summary.fetched += 1
            return
        if out.kind in ("premium_new", "premium_granted"):
            prem = registry.move_to_premium(item.token, out.http_status, now)
            summary.premium_detected += 1
            if out.kind == "premium_granted":
                _write_cache_file(root, prem, out.html, recipient)
                prem.update(status=True, file=crypto.cache_filename(prem["sha"]),
                            fetched_at=now, http_status=out.http_status)
                summary.premium_fetched += 1
            return
        rec["last_checked_at"] = now
        rec["http_status"] = out.http_status
        rec["fetch_failures"] = int(rec.get("fetch_failures", 0)) + 1
        summary.failed += 1
        return

    if out.kind == "premium_denied":
        denied = registry.move_to_denied(item.token, out.http_status, now)
        summary.denied_shas.append(denied["sha"])
        summary.premium_denied += 1
        return

    rec = registry.premium[item.token]
    rec["last_checked_at"] = now
    rec["http_status"] = out.http_status
    if out.kind == "premium_granted":
        _write_cache_file(root, rec, out.html, recipient)
        rec.update(status=True, file=crypto.cache_filename(rec["sha"]), fetched_at=now)
        summary.premium_fetched += 1
    else:
        summary.failed += 1


def _raise_interrupt(signum, frame):  # SIGTERM (runner cancel/timeout) -> same path as Ctrl-C
    raise KeyboardInterrupt


def main() -> int:
    setup_logging()
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    try:
        summary = run_update(cfg, ROOT)
    except KeyboardInterrupt:
        log.warning("Interrupted; progress so far was saved.")
        logging.shutdown()
        os._exit(130)   # do not wait for in-flight requests; the workflow commits what was saved
    except (crypto.CryptoError, storage.StorageError) as exc:
        log.error("Update stopped safely (saved state is intact): %s", exc)
        return 1
    return 3 if summary.blocked_stop else 0


if __name__ == "__main__":
    sys.exit(main())