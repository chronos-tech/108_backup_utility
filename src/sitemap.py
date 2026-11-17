"""WordPress core sitemap parsing (``wp-sitemap.xml`` index + child sitemaps)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .utils import normalize_url, same_site

log = logging.getLogger(__name__)

# wp-sitemap-posts-post-<n>.xml / wp-sitemap-posts-page-<n>.xml, any numeric suffix.
CHILD_RE = re.compile(r"^wp-sitemap-posts-(post|page)-\d+\.xml$")


class SitemapError(Exception):
    """Sitemap could not be fetched or parsed. Messages never include URLs."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(content: bytes) -> Element:
    try:
        return SafeET.fromstring(content)
    except (SafeET.ParseError, DefusedXmlException, ValueError) as exc:
        raise SitemapError("invalid or unsafe sitemap XML") from exc


def _locs(root: Element, parent: str) -> list[str]:
    out: list[str] = []
    for node in root:
        if _local(node.tag) != parent:
            continue
        for child in node:
            if _local(child.tag) == "loc" and child.text and child.text.strip():
                out.append(child.text.strip())
    return out


def parse_sitemap_index(content: bytes) -> list[str]:
    """Return every child sitemap URL listed in a ``<sitemapindex>``."""
    root = _parse(content)
    if _local(root.tag) != "sitemapindex":
        raise SitemapError("expected a <sitemapindex> document")
    return _locs(root, "sitemap")


def parse_urlset(content: bytes) -> list[str]:
    """Return every page URL listed in a ``<urlset>``."""
    root = _parse(content)
    if _local(root.tag) != "urlset":
        raise SitemapError("expected a <urlset> document")
    return _locs(root, "url")


def filter_child_sitemaps(locs: list[str], base_url: str) -> list[tuple[str, str]]:
    """Keep only post/page sitemaps on the configured site.

    Returns ``[(sitemap_url, "post"|"page")]`` sorted and de-duplicated.
    Categories, tags, authors, taxonomies and anything else are ignored.
    """
    base_host = urlsplit(base_url).hostname
    seen: dict[str, str] = {}
    for loc in locs:
        try:
            parts = urlsplit(loc)
            host = parts.hostname
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not same_site(host, base_host):
            continue
        match = CHILD_RE.match(parts.path.rsplit("/", 1)[-1])
        if match and loc not in seen:
            seen[loc] = match.group(1)
    return sorted(seen.items())


@dataclass
class UrlCollection:
    """Normalized, de-duplicated URLs; the first sitemap to list a URL fixes its type."""

    base_url: str
    urls: dict[str, str] = field(default_factory=dict)  # normalized URL -> type
    invalid: int = 0
    offsite: int = 0
    duplicates: int = 0

    def add_all(self, raw_urls: list[str], page_type: str) -> None:
        base_host = urlsplit(self.base_url).hostname
        for raw in raw_urls:
            try:
                url = normalize_url(raw)
            except ValueError:
                self.invalid += 1
                continue
            if not same_site(urlsplit(url).hostname, base_host):
                self.offsite += 1
                continue
            if url in self.urls:
                self.duplicates += 1
                continue
            self.urls[url] = page_type
