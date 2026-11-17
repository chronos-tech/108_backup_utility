import pytest
import responses

from src import fetcher
from src.fetcher import FetchResult, RobotsPolicy, Throttle
from tests.conftest import BASE, PAGE, PREMIUM_PAGE, make_cfg

U = f"{BASE}/a/"


@responses.activate
def test_retries_then_succeeds_with_backoff(client_factory):
    responses.add(responses.GET, U, status=503)
    responses.add(responses.GET, U, status=500)
    responses.add(responses.GET, U, body=PAGE, content_type="text/html")
    client = client_factory(make_cfg())
    res = client.get(U)
    assert res.ok and len(responses.calls) == 3
    assert len(client.sleeps) == 2 and client.sleeps[1] > client.sleeps[0] >= 2.0


@responses.activate
def test_gives_up_after_three_retries(client_factory):
    responses.add(responses.GET, U, status=500)
    res = client_factory(make_cfg()).get(U)
    assert res.error == "http_500" and len(responses.calls) == 4


@responses.activate
def test_retry_after_respected(client_factory):
    responses.add(responses.GET, U, status=429, headers={"Retry-After": "30"})
    responses.add(responses.GET, U, body=PAGE, content_type="text/html")
    client = client_factory(make_cfg())
    assert client.get(U).ok
    assert client.sleeps and client.sleeps[0] >= 30


@responses.activate
def test_huge_retry_after_means_stop(client_factory):
    responses.add(responses.GET, U, status=429, headers={"Retry-After": "86400"})
    res = client_factory(make_cfg()).get(U)
    assert res.blocked and len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_not_retried(client_factory, status):
    responses.add(responses.GET, U, status=status)
    res = client_factory(make_cfg()).get(U)
    assert res.blocked and len(responses.calls) == 1


@responses.activate
def test_404_not_retried_not_blocked(client_factory):
    responses.add(responses.GET, U, status=404)
    res = client_factory(make_cfg()).get(U)
    assert not res.blocked and res.error == "http_404" and len(responses.calls) == 1


@responses.activate
def test_connection_error_retried(client_factory):
    import requests
    responses.add(responses.GET, U, body=requests.ConnectionError("boom"))
    res = client_factory(make_cfg()).get(U)
    assert res.error == "connection" and len(responses.calls) == 4


@responses.activate
def test_offsite_and_login_redirects_refused(client_factory):
    responses.add(responses.GET, U, status=302, headers={"Location": "https://evil.test/x"})
    assert client_factory(make_cfg()).get(U).error == "offsite_redirect"
    responses.reset()
    responses.add(responses.GET, U, status=302, headers={"Location": f"{BASE}/wp-login.php?x=1"})
    assert client_factory(make_cfg()).get(U).error == "login_redirect"
    responses.reset()
    responses.add(responses.GET, U, status=302, headers={"Location": "http://example.test/a/"})
    assert client_factory(make_cfg()).get(U).error == "downgrade_redirect"


@responses.activate
def test_same_site_redirect_followed_cookie_only_when_asked(client_factory):
    responses.add(responses.GET, U, status=301, headers={"Location": f"{BASE}/b/"})
    responses.add(responses.GET, f"{BASE}/b/", body=PAGE, content_type="text/html")
    cfg = make_cfg(COOKIE="s=abc")
    client = client_factory(cfg)
    assert client.get(U).ok
    assert all("Cookie" not in c.request.headers for c in responses.calls)
    assert client.get(U, use_cookie=True).ok
    assert responses.calls[-1].request.headers["Cookie"] == "s=abc"
    assert responses.calls[-1].request.headers["User-Agent"].startswith("wp-sitemap-cache/")


@responses.activate
def test_body_size_cap(client_factory):
    responses.add(responses.GET, U, body=b"x" * 5000, content_type="text/html")
    assert client_factory(make_cfg()).get(U, max_bytes=1000).error == "too_large"


def test_validate_html_and_marker():
    ok = FetchResult(status=200, body=PAGE, content_type="text/html; charset=UTF-8")
    assert fetcher.validate_html(ok) is None
    assert fetcher.validate_html(FetchResult(status=200, body=PAGE, content_type="application/json")) == "not_html"
    assert fetcher.validate_html(FetchResult(status=200, body=b"<html></html>", content_type="text/html")) == "too_short"
    assert fetcher.validate_html(FetchResult(status=200, body=b"z" * 500, content_type="text/html")) == "not_html"
    assert fetcher.validate_html(FetchResult(status=404, error="http_404")) == "http_404"
    assert fetcher.has_premium_marker(PREMIUM_PAGE) and not fetcher.has_premium_marker(PAGE)


def test_retry_after_parsing():
    assert fetcher.parse_retry_after("12") == 12.0
    assert fetcher.parse_retry_after(None) is None and fetcher.parse_retry_after("soon") is None
    assert fetcher.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_throttle_spacing_is_global():
    now, slept = [100.0], []
    t = Throttle(2, 2, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                 monotonic=lambda: now[0], rand=lambda a, b: a)
    t.wait(); t.wait(); t.wait()
    assert slept == [2, 2]


@responses.activate
def test_robots_rules(client_factory):
    cfg = make_cfg()
    responses.add(responses.GET, f"{BASE}/robots.txt",
                  body="User-agent: *\nDisallow: /private/\nCrawl-delay: 7\n")
    client = client_factory(cfg)
    robots = RobotsPolicy(client, cfg.base_url, cfg.user_agent)
    assert robots.allowed(f"{BASE}/a/") and not robots.allowed(f"{BASE}/private/x/")
    assert client.throttle.delay_min == 7


@responses.activate
def test_robots_404_allows_5xx_blocks(client_factory):
    cfg = make_cfg()
    responses.add(responses.GET, f"{BASE}/robots.txt", status=404)
    assert RobotsPolicy(client_factory(cfg), cfg.base_url, cfg.user_agent).allowed(U)
    responses.reset()
    responses.add(responses.GET, f"{BASE}/robots.txt", status=500)
    assert not RobotsPolicy(client_factory(cfg), cfg.base_url, cfg.user_agent).allowed(U)
