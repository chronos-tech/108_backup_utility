#!/usr/bin/env python3
"""Discover WordPress post/page URLs and register new ones (append-only).

Exit codes: 0 ok, 1 fatal (crypto/storage/sitemap), 2 bad configuration,
3 finished but some child sitemaps could not be fetched.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from src import crypto, sitemap, storage
from src.config import Config, ConfigError
from src.fetcher import HttpClient
from src.sitemap import SitemapError, UrlCollection
from src.utils import setup_logging

log = logging.getLogger("refresh")
ROOT = Path(__file__).resolve().parent


@dataclass
class RefreshResult:
    sitemaps_found: int = 0
    sitemaps_failed: int = 0
    discovered: int = 0
    registered: int = 0
    already_known: int = 0


def run_refresh(cfg: Config, root: Path, client: HttpClient | None = None) -> RefreshResult:
    key, _ = crypto.validate_crypto_config(cfg.aes_key)
    client = client or HttpClient(cfg)
    result = RefreshResult()

    with storage.file_lock(root):
        registry = storage.Registry.load(root, key)
        before_cache, before_premium = registry.snapshot()

        index = client.get(f"{cfg.base_url}/wp-sitemap.xml")
        if not index.ok:
            raise SitemapError("could not fetch the sitemap index")
        children = sitemap.filter_child_sitemaps(
            sitemap.parse_sitemap_index(index.body), cfg.base_url)
        if not children:
            raise SitemapError("no post/page child sitemaps found in the index")
        result.sitemaps_found = len(children)

        collected = UrlCollection(cfg.base_url)
        for child_url, page_type in children:
            res = client.get(child_url)
            if not res.ok:
                result.sitemaps_failed += 1
                continue
            try:
                collected.add_all(sitemap.parse_urlset(res.body), page_type)
            except SitemapError:
                result.sitemaps_failed += 1

        result.discovered = len(collected.urls)
        for url, page_type in collected.urls.items():
            if registry.add_new(url, page_type):
                result.registered += 1
            else:
                result.already_known += 1

        # Prove append-only before touching disk.
        storage.verify_append_only(before_cache, registry.cache, "cache.json")
        storage.verify_append_only(before_premium, registry.premium, "premium.json")
        registry.save()

    log.info("Found %d post/page sitemaps (%d failed).", result.sitemaps_found, result.sitemaps_failed)
    log.info("Discovered %d sitemap URLs.", result.discovered)
    log.info("Registered %d new URLs.", result.registered)
    log.info("Already known: %d.", result.already_known)
    return result


def main() -> int:
    setup_logging()
    try:
        cfg = Config.from_env(require_age_recipient=False)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    try:
        result = run_refresh(cfg, ROOT)
    except (crypto.CryptoError, storage.StorageError, SitemapError) as exc:
        log.error("Refresh aborted, no data changed: %s", exc)
        return 1
    return 3 if result.sitemaps_failed else 0


if __name__ == "__main__":
    sys.exit(main())
