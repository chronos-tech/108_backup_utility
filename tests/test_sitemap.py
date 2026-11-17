import pytest

from src import sitemap
from src.sitemap import SitemapError, UrlCollection

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def index(*locs):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'.encode()


def urlset(*locs):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return f'<?xml version="1.0"?><urlset {NS}>{body}</urlset>'.encode()


B = "https://example.test"


def test_index_parsing_and_filtering_any_suffix():
    locs = sitemap.parse_sitemap_index(index(
        f"{B}/wp-sitemap-posts-post-1.xml", f"{B}/wp-sitemap-posts-post-17.xml",
        f"{B}/wp-sitemap-posts-page-1.xml", f"{B}/wp-sitemap-posts-page-300.xml",
        f"{B}/wp-sitemap-taxonomies-category-1.xml", f"{B}/wp-sitemap-taxonomies-post_tag-1.xml",
        f"{B}/wp-sitemap-users-1.xml", f"{B}/wp-sitemap-posts-product-1.xml",
        f"{B}/wp-sitemap-posts-post-x.xml", "https://evil.test/wp-sitemap-posts-post-1.xml",
    ))
    kept = sitemap.filter_child_sitemaps(locs, B)
    assert [(u.rsplit("/", 1)[1], t) for u, t in kept] == [
        ("wp-sitemap-posts-page-1.xml", "page"), ("wp-sitemap-posts-page-300.xml", "page"),
        ("wp-sitemap-posts-post-1.xml", "post"), ("wp-sitemap-posts-post-17.xml", "post"),
    ]


def test_www_variant_accepted():
    kept = sitemap.filter_child_sitemaps(["https://www.example.test/wp-sitemap-posts-post-1.xml"], B)
    assert len(kept) == 1


def test_wrong_root_and_invalid_xml():
    with pytest.raises(SitemapError):
        sitemap.parse_sitemap_index(urlset(f"{B}/a/"))
    with pytest.raises(SitemapError):
        sitemap.parse_urlset(index(f"{B}/x.xml"))
    with pytest.raises(SitemapError):
        sitemap.parse_urlset(b"<urlset><url>")


def test_entity_expansion_rejected():
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><urlset><url><loc>&b;</loc></url></urlset>'
    with pytest.raises(SitemapError):
        sitemap.parse_urlset(bomb)


def test_dedup_normalization_and_offsite():
    col = UrlCollection(B)
    col.add_all(sitemap.parse_urlset(urlset(
        f"{B}/a/", f"{B}/a/#frag", "HTTPS://EXAMPLE.TEST/a/", f"{B}/b/",
        "https://other.test/c/", "ftp://example.test/d/", "not a url")), "post")
    col.add_all([f"{B}/a/", f"{B}/p/"], "page")
    assert col.urls == {f"{B}/a/": "post", f"{B}/b/": "post", f"{B}/p/": "page"}
    assert col.offsite == 1 and col.invalid == 2 and col.duplicates >= 3
