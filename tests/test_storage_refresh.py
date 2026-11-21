import json

import pytest
import responses

from refresh import run_refresh
from src import crypto, storage
from src.sitemap import SitemapError
from tests.conftest import AES_HEX, BASE, make_cfg, read_json
from tests.test_sitemap import index, urlset

KEY = crypto.parse_aes_key(AES_HEX)


def mock_site(posts=("a", "b"), pages=("about",), extra_child=True):
    kids = [f"{BASE}/wp-sitemap-posts-post-1.xml", f"{BASE}/wp-sitemap-posts-page-1.xml"]
    if extra_child:
        kids.append(f"{BASE}/wp-sitemap-taxonomies-category-1.xml")
    responses.add(responses.GET, f"{BASE}/wp-sitemap.xml", body=index(*kids))
    responses.add(responses.GET, kids[0], body=urlset(*[f"{BASE}/{p}/" for p in posts]))
    responses.add(responses.GET, kids[1], body=urlset(*[f"{BASE}/{p}/" for p in pages]))


def all_text(root):
    return "".join(p.read_text() for p in (root / "data").glob("*.json"))


@responses.activate
def test_refresh_registers_new_only_and_hides_urls(root, client_factory):
    mock_site()
    cfg = make_cfg()
    res = run_refresh(cfg, root, client_factory(cfg))
    assert (res.discovered, res.registered, res.sitemaps_found) == (3, 3, 2)
    data = read_json(root, "cache.json")
    assert len(data) == 3 and all(k.startswith("v1.") for k in data)
    text = all_text(root)
    for needle in ("example", "/a/", "about"):
        assert needle not in text
    rec = next(iter(data.values()))
    assert rec["status"] is False and rec["file"] == f"cache/{rec['sha']}.html.age"
    # the ignored taxonomy sitemap was never requested
    assert not any("taxonomies" in c.request.url for c in responses.calls)


@responses.activate
def test_refresh_is_append_only(root, client_factory):
    cfg = make_cfg()
    mock_site(posts=("a", "b"), pages=())
    run_refresh(cfg, root, client_factory(cfg))
    before = read_json(root, "cache.json")
    # mutate one record as if update.py had processed it
    token = next(iter(before))
    before[token].update(status=True, fetched_at="2026-01-01T00:00:00Z", http_status=200)
    (root / "data" / "cache.json").write_text(json.dumps(before))
    snapshot = json.loads(json.dumps(before))

    responses.reset()
    mock_site(posts=("b", "c"), pages=())      # "a" disappeared, "c" is new
    res = run_refresh(cfg, root, client_factory(cfg))
    after = read_json(root, "cache.json")
    assert res.registered == 1 and len(after) == 3
    for tok, rec in snapshot.items():          # nothing removed or changed
        assert after[tok] == rec

    responses.reset()
    mock_site(posts=("b", "c"), pages=())
    assert run_refresh(cfg, root, client_factory(cfg)).registered == 0
    assert read_json(root, "cache.json") == after


@responses.activate
def test_refresh_skips_urls_already_in_premium(root, client_factory):
    cfg = make_cfg()
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/vip/", "post")
    reg.move_to_premium(next(iter(reg.cache)), 200, "2026-01-01T00:00:00Z")
    reg.save()
    premium_before = read_json(root, "premium.json")
    mock_site(posts=("vip", "new"), pages=())
    res = run_refresh(cfg, root, client_factory(cfg))
    assert res.registered == 1 and read_json(root, "premium.json") == premium_before
    assert read_json(root, "cache.json") == {} or len(read_json(root, "cache.json")) == 1


@responses.activate
def test_refresh_no_matching_sitemaps_is_an_error(root, client_factory):
    responses.add(responses.GET, f"{BASE}/wp-sitemap.xml",
                  body=index(f"{BASE}/wp-sitemap-users-1.xml"))
    cfg = make_cfg()
    with pytest.raises(SitemapError):
        run_refresh(cfg, root, client_factory(cfg))
    assert read_json(root, "cache.json") == {}


@responses.activate
def test_refresh_child_failure_still_registers_rest(root, client_factory):
    responses.add(responses.GET, f"{BASE}/wp-sitemap.xml", body=index(
        f"{BASE}/wp-sitemap-posts-post-1.xml", f"{BASE}/wp-sitemap-posts-post-2.xml"))
    responses.add(responses.GET, f"{BASE}/wp-sitemap-posts-post-1.xml", body=urlset(f"{BASE}/a/"))
    responses.add(responses.GET, f"{BASE}/wp-sitemap-posts-post-2.xml", status=404)
    cfg = make_cfg()
    res = run_refresh(cfg, root, client_factory(cfg))
    assert res.registered == 1 and res.sitemaps_failed == 1


# ------------------------------------------------ corrupted metadata protection

@pytest.mark.parametrize("content", ["{not json", "[]", '{"a": 1, "a": 2}', "", "null"])
def test_corrupt_json_is_fatal_and_untouched(root, content):
    path = root / "data" / "cache.json"
    path.write_text(content)
    with pytest.raises(storage.StorageError):
        storage.Registry.load(root, KEY)
    assert path.read_text() == content


def test_missing_file_is_fatal(root):
    (root / "data" / "premium.json").unlink()
    with pytest.raises(storage.StorageError):
        storage.Registry.load(root, KEY)


def test_wrong_aes_key_stops_without_writing(root):
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/a/", "post")
    reg.save()
    before = (root / "data" / "cache.json").read_bytes()
    with pytest.raises(crypto.CryptoError):
        storage.Registry.load(root, crypto.parse_aes_key("11" * 32))
    assert (root / "data" / "cache.json").read_bytes() == before


def test_sha_mismatch_and_bad_schema_detected(root):
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/a/", "post")
    reg.save()
    data = read_json(root, "cache.json")
    token = next(iter(data))
    for field, value in (("sha", "0" * 64), ("status", "yes"), ("type", "video"),
                         ("file", "cache/other.html.age")):
        bad = json.loads(json.dumps(data))
        bad[token][field] = value
        (root / "data" / "cache.json").write_text(json.dumps(bad))
        with pytest.raises(storage.StorageError):
            storage.Registry.load(root, KEY)


def test_same_url_under_two_tokens_is_fatal(root):
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/a/", "post")
    reg.save()
    data = read_json(root, "cache.json")
    rec = next(iter(data.values()))
    data[crypto.encrypt_url(KEY, f"{BASE}/a/")] = dict(rec)
    (root / "data" / "cache.json").write_text(json.dumps(data))
    with pytest.raises(storage.StorageError):
        storage.Registry.load(root, KEY)


def test_interrupted_premium_move_recovers(root):
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/a/", "post")
    reg.save()
    stale_cache = (root / "data" / "cache.json").read_text()
    reg.move_to_premium(next(iter(reg.cache)), 200, "2026-01-01T00:00:00Z")
    reg.save()
    (root / "data" / "cache.json").write_text(stale_cache)   # crash after premium write
    again = storage.Registry.load(root, KEY)
    assert len(again.premium) == 1 and len(again.cache) == 0


def test_verify_append_only_detects_changes():
    before = {"t": {"status": False}}
    storage.verify_append_only(before, {"t": {"status": False}, "u": {}}, "x")
    with pytest.raises(storage.StorageError):
        storage.verify_append_only(before, {}, "x")
    with pytest.raises(storage.StorageError):
        storage.verify_append_only(before, {"t": {"status": True}}, "x")


def test_atomic_write_leaves_no_temp_files(root):
    reg = storage.Registry.load(root, KEY)
    reg.add_new(f"{BASE}/a/", "post")
    reg.save()
    assert not list((root / "data").glob(".tmp-*"))


@pytest.mark.skipif(storage.fcntl is None, reason="fcntl unavailable on Windows")
def test_lock_blocks_second_holder(root):
    with storage.file_lock(root):
        with pytest.raises(storage.StorageError):
            with storage.file_lock(root):
                pass
