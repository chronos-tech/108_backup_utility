import pyrage
import pytest
import responses

from src import crypto, storage
from tests.conftest import AES_HEX, BASE, IDENTITY, PAGE, PREMIUM_PAGE, make_cfg, read_json
from update import run_update

KEY = crypto.parse_aes_key(AES_HEX)


def setup_urls(root, slugs, page_type="post"):
    reg = storage.Registry.load(root, KEY)
    for s in slugs:
        reg.add_new(f"{BASE}/{s}/", page_type)
    reg.save()
    return reg


def page_url(slug):
    return f"{BASE}/{slug}/"


def cached_path(root, slug):
    return root / crypto.cache_filename(crypto.url_sha256(page_url(slug)))


def mock_robots():
    responses.add(responses.GET, f"{BASE}/robots.txt", status=404)


def mock_pages(slugs, body=PAGE):
    for s in slugs:
        responses.add(responses.GET, page_url(s), body=body, content_type="text/html")


def statuses(root):
    return sorted(r["status"] for r in read_json(root, "cache.json").values())


@responses.activate
def test_fetch_encrypt_and_metadata(root, client_factory):
    setup_urls(root, ["a", "b"])
    mock_robots(); mock_pages(["a", "b"])
    cfg = make_cfg()
    summary = run_update(cfg, root, client_factory(cfg))
    assert summary.fetched == 2 and statuses(root) == [True, True]
    blob = cached_path(root, "a").read_bytes()
    assert blob.startswith(crypto.AGE_HEADER) and b"<html" not in blob
    assert pyrage.decrypt(blob, [IDENTITY]) == PAGE
    rec = next(iter(read_json(root, "cache.json").values()))
    assert rec["http_status"] == 200 and rec["fetched_at"].endswith("Z")
    assert not list(root.rglob("*.html"))
    assert "example" not in (root / "data" / "cache.json").read_text()


@responses.activate
def test_skip_conditions(root, client_factory):
    setup_urls(root, ["a", "b"])
    mock_robots(); mock_pages(["a", "b"])
    cfg = make_cfg()
    run_update(cfg, root, client_factory(cfg))
    calls = len(responses.calls)
    # status true + file exists -> skipped, no requests at all
    s = run_update(cfg, root, client_factory(cfg))
    assert s.skipped_cached == 2 and s.fetched == 0 and len(responses.calls) == calls
    # status true + file missing -> fetched again
    cached_path(root, "a").unlink()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.fetched == 1 and s.skipped_cached == 1 and cached_path(root, "a").exists()


@responses.activate
def test_failed_pages_stay_eligible(root, client_factory):
    setup_urls(root, ["a"])
    mock_robots()
    responses.add(responses.GET, page_url("a"), status=404)
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.failed == 1 and statuses(root) == [False]
    rec = next(iter(read_json(root, "cache.json").values()))
    assert rec["http_status"] == 404 and rec["fetch_failures"] == 1
    responses.reset(); mock_robots(); mock_pages(["a"])
    assert run_update(cfg, root, client_factory(cfg)).fetched == 1


@responses.activate
def test_invalid_content_not_cached(root, client_factory):
    setup_urls(root, ["a", "b"])
    mock_robots()
    responses.add(responses.GET, page_url("a"), body=b'{"json": true}' * 50, content_type="application/json")
    responses.add(responses.GET, page_url("b"), body=b"<html></html>", content_type="text/html")
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.failed == 2 and not list((root / "cache").glob("*.age"))


# ------------------------------------------------ download all vs first 10 valid

@responses.activate
def test_download_all_default(root, client_factory):
    slugs = [f"p{i}" for i in range(15)]
    setup_urls(root, slugs); mock_robots(); mock_pages(slugs)
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.fetched == 15 and statuses(root) == [True] * 15


@responses.activate
def test_limit_stops_after_exactly_10_valid(root, client_factory):
    slugs = [f"p{i:02d}" for i in range(25)]
    setup_urls(root, slugs); mock_robots(); mock_pages(slugs)
    cfg = make_cfg(MAX_VALID_PAGES="10")
    s = run_update(cfg, root, client_factory(cfg))
    assert s.fetched == 10 and statuses(root).count(True) == 10
    assert len(list((root / "cache").glob("*.age"))) == 10
    assert s.deferred > 0
    # next limited run continues with the next 10, never re-fetching cached ones
    s2 = run_update(cfg, root, client_factory(cfg))
    assert s2.fetched == 10 and statuses(root).count(True) == 20 and s2.skipped_cached == 10


@responses.activate
def test_limit_counts_only_valid_pages(root, client_factory):
    bad = [f"bad{i}" for i in range(6)]
    good = [f"ok{i}" for i in range(12)]
    setup_urls(root, bad + good); mock_robots(); mock_pages(good)
    for s in bad:
        responses.add(responses.GET, page_url(s), status=404)
    cfg = make_cfg(MAX_VALID_PAGES="10", MAX_WORKERS="1")
    s = run_update(cfg, root, client_factory(cfg))
    assert s.fetched == 10 and statuses(root).count(True) == 10


# ----------------------------------------------------------------- premium

@responses.activate
def test_premium_detected_not_cached_and_not_refetched(root, client_factory):
    setup_urls(root, ["vip", "free"])
    mock_robots(); mock_pages(["vip"], PREMIUM_PAGE); mock_pages(["free"])
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.premium_detected == 1 and s.fetched == 1
    prem = read_json(root, "premium.json")
    assert len(prem) == 1
    rec = next(iter(prem.values()))
    assert rec["status"] is False and rec["file"] is None and rec["detected_at"]
    assert not cached_path(root, "vip").exists()
    assert len(read_json(root, "cache.json")) == 1          # moved out of cache.json
    assert "vip" not in (root / "data" / "premium.json").read_text()
    calls = len(responses.calls)
    s = run_update(cfg, root, client_factory(cfg))          # scheduled-style rerun
    assert len(responses.calls) == calls and s.skipped_premium == 1


@responses.activate
def test_premium_true_without_cookie_does_not_fetch(root, client_factory):
    setup_urls(root, ["vip"]); mock_robots(); mock_pages(["vip"], PREMIUM_PAGE)
    cfg = make_cfg(FETCH_PREMIUM="true")
    run_update(cfg, root, client_factory(cfg))
    calls = len(responses.calls)
    s = run_update(cfg, root, client_factory(cfg))
    assert len(responses.calls) == calls and s.skipped_premium == 1
    assert all("Cookie" not in c.request.headers for c in responses.calls)


def _auth_callback(granted):
    def cb(request):
        if "Cookie" in request.headers:
            body = PAGE if granted else PREMIUM_PAGE
        else:
            body = PREMIUM_PAGE
        return 200, {"Content-Type": "text/html"}, body
    return cb


@responses.activate
@pytest.mark.parametrize("granted", [True, False])
def test_premium_true_with_cookie(root, client_factory, granted):
    setup_urls(root, ["vip"]); mock_robots()
    responses.add_callback(responses.GET, page_url("vip"), callback=_auth_callback(granted))
    cfg = make_cfg(FETCH_PREMIUM="true", COOKIE="session=TESTONLY")
    s = run_update(cfg, root, client_factory(cfg))
    rec = next(iter(read_json(root, "premium.json").values()))
    assert read_json(root, "cache.json") == {} and s.premium_detected == 1
    if granted:
        assert rec["status"] is True and rec["file"] and cached_path(root, "vip").exists()
        assert pyrage.decrypt(cached_path(root, "vip").read_bytes(), [IDENTITY]) == PAGE
        assert s.premium_fetched == 1
    else:                                   # access denied: record kept, nothing cached
        assert rec["status"] is False and rec["file"] is None
        assert not cached_path(root, "vip").exists()
    # the anonymous probe never carried the cookie
    assert "Cookie" not in responses.calls[1].request.headers


@responses.activate
def test_existing_premium_record_retried_only_when_enabled(root, client_factory):
    reg = setup_urls(root, ["vip"])
    reg.move_to_premium(next(iter(reg.cache)), 200, "2026-01-01T00:00:00Z"); reg.save()
    mock_robots()
    responses.add_callback(responses.GET, page_url("vip"), callback=_auth_callback(True))
    run_update(make_cfg(), root, client_factory(make_cfg()))
    assert len(responses.calls) == 0                       # disabled -> no traffic
    cfg = make_cfg(FETCH_PREMIUM="true", COOKIE="session=TESTONLY")
    s = run_update(cfg, root, client_factory(cfg))
    assert s.premium_fetched == 1 and cached_path(root, "vip").exists()
    assert next(iter(read_json(root, "premium.json").values()))["status"] is True


@responses.activate
def test_login_redirect_is_not_success(root, client_factory):
    setup_urls(root, ["a"]); mock_robots()
    responses.add(responses.GET, page_url("a"), status=302, headers={"Location": f"{BASE}/wp-login.php"})
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.failed == 1 and not list((root / "cache").glob("*.age"))


# ---------------------------------------------------------- safety behaviours

@responses.activate
def test_robots_disallowed_pages_skipped(root, client_factory):
    setup_urls(root, ["a", "b"])
    responses.add(responses.GET, f"{BASE}/robots.txt", body=f"User-agent: *\nDisallow: /a/\n")
    mock_pages(["b"])
    cfg = make_cfg()
    s = run_update(cfg, root, client_factory(cfg))
    assert s.robots_skipped == 1 and s.fetched == 1
    assert not any(c.request.url == page_url("a") for c in responses.calls)


@responses.activate
def test_repeated_blocks_stop_the_run(root, client_factory):
    slugs = [f"p{i:02d}" for i in range(30)]
    setup_urls(root, slugs); mock_robots()
    for s in slugs:
        responses.add(responses.GET, page_url(s), status=403)
    cfg = make_cfg(MAX_WORKERS="1")
    s = run_update(cfg, root, client_factory(cfg))
    page_calls = [c for c in responses.calls if "robots" not in c.request.url]
    assert s.blocked_stop and len(page_calls) < 30


def test_corrupted_metadata_stops_before_any_request(root, client_factory):
    (root / "data" / "cache.json").write_text("{broken")
    cfg = make_cfg()
    with responses.RequestsMock() as rsps:
        with pytest.raises(storage.StorageError):
            run_update(cfg, root, client_factory(cfg))
        assert len(rsps.calls) == 0
    assert (root / "data" / "cache.json").read_text() == "{broken"


def test_bad_crypto_config_stops_early(root, client_factory):
    with pytest.raises(crypto.CryptoError):
        run_update(make_cfg(AES_KEY="bad"), root, client_factory(make_cfg()))
    with pytest.raises(crypto.CryptoError):
        run_update(make_cfg(AGE_RECIPIENT="age1nope"), root, client_factory(make_cfg()))


@responses.activate
def test_no_plaintext_in_logs(root, client_factory, caplog):
    setup_urls(root, ["secret-slug"]); mock_robots(); mock_pages(["secret-slug"])
    cfg = make_cfg(COOKIE="session=TESTONLY")
    with caplog.at_level("DEBUG"):
        run_update(cfg, root, client_factory(cfg))
    text = caplog.text
    for needle in ("secret-slug", "example.test", "TESTONLY", AES_HEX, "<html"):
        assert needle not in text


# ------------------------------------------------------ interruption handling

def test_runstate_deadline_and_stop():
    import time
    from update import RunState
    assert not RunState(0, time.monotonic() + 60).should_stop()
    assert RunState(0, time.monotonic() - 1).should_stop()


@responses.activate
def test_interrupt_saves_progress(root, client_factory, monkeypatch):
    import update
    slugs = [f"p{i}" for i in range(6)]
    setup_urls(root, slugs); mock_robots(); mock_pages(slugs)
    real = update.as_completed

    def interrupted(futures):
        gen = real(futures)
        yield next(gen); yield next(gen)
        raise KeyboardInterrupt          # what SIGTERM/SIGINT turn into

    monkeypatch.setattr(update, "as_completed", interrupted)
    cfg = make_cfg(MAX_WORKERS="1")
    with pytest.raises(KeyboardInterrupt):
        run_update(cfg, root, client_factory(cfg))
    reg = storage.Registry.load(root, KEY)             # file is valid JSON, consistent
    done = [r for r in reg.cache.values() if r["status"]]
    assert len(done) >= 2 and all(reg.cache_file_exists(r) for r in done)


@responses.activate
def test_runtime_deadline_stops_cleanly(root, client_factory, monkeypatch):
    import update
    slugs = [f"p{i}" for i in range(8)]
    setup_urls(root, slugs); mock_robots(); mock_pages(slugs)
    ticks = iter([0.0] + [10_000.0] * 1000)          # deadline passes right after start
    monkeypatch.setattr(update.time, "monotonic", lambda: next(ticks))
    cfg = make_cfg(MAX_RUNTIME_MINUTES="1", MAX_WORKERS="1")
    s = run_update(cfg, root, client_factory(cfg))
    assert s.fetched == 0 and s.deferred == 8
    assert statuses(root) == [False] * 8
