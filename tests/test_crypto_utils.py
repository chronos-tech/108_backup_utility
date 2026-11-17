import pyrage
import pytest

from src import crypto
from src.config import Config, ConfigError
from src.utils import normalize_url, same_site
from tests.conftest import AES_HEX, IDENTITY, make_env


@pytest.fixture
def key():
    return crypto.parse_aes_key(AES_HEX)


def test_aes_roundtrip(key):
    url = "https://example.test/a-post/"
    assert crypto.decrypt_url(key, crypto.encrypt_url(key, url)) == url


def test_fresh_nonce_each_time(key):
    url = "https://example.test/a-post/"
    tokens = {crypto.encrypt_url(key, url) for _ in range(20)}
    assert len(tokens) == 20
    assert all(crypto.decrypt_url(key, t) == url for t in tokens)


def test_token_is_json_key_safe_and_hides_url(key):
    token = crypto.encrypt_url(key, "https://example.test/secret-slug/")
    assert token.startswith("v1.") and "secret-slug" not in token and "example" not in token


def test_wrong_key_and_tamper_rejected(key):
    token = crypto.encrypt_url(key, "https://example.test/x/")
    other = crypto.parse_aes_key("11" * 32)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_url(other, token)
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_url(key, flipped)
    for bad in ("", "v1.", "v2.abc", "nonsense", "v1.!!!"):
        with pytest.raises(crypto.CryptoError):
            crypto.decrypt_url(key, bad)


@pytest.mark.parametrize("bad", ["", "   ", "abc", "zz" * 32, "00" * 31, "00" * 33])
def test_invalid_aes_key(bad):
    with pytest.raises(crypto.CryptoError):
        crypto.parse_aes_key(bad)


def test_aes_key_base64_accepted():
    import base64
    assert len(crypto.parse_aes_key(base64.b64encode(bytes(32)).decode())) == 32


def test_invalid_age_recipient():
    with pytest.raises(crypto.CryptoError):
        crypto.validate_crypto_config(AES_HEX, "not-an-age-key")
    with pytest.raises(crypto.CryptoError):
        crypto.validate_crypto_config("short", None)


def test_validate_ok():
    key, rec = crypto.validate_crypto_config(AES_HEX, str(IDENTITY.to_public()))
    assert len(key) == 32 and rec is not None


def test_sha_filename():
    sha = crypto.url_sha256("https://example.test/a/")
    assert len(sha) == 64 and crypto.cache_filename(sha) == f"cache/{sha}.html.age"
    assert "example" not in crypto.cache_filename(sha)
    with pytest.raises(crypto.CryptoError):
        crypto.cache_filename("../../etc/passwd")


def test_age_roundtrip():
    recipient = crypto.parse_age_recipient(str(IDENTITY.to_public()))
    blob = crypto.encrypt_html(b"<html>hello</html>", recipient)
    assert blob.startswith(crypto.AGE_HEADER) and b"hello" not in blob
    assert pyrage.decrypt(blob, [IDENTITY]) == b"<html>hello</html>"


@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.TEST:443/Path/?q=1#frag", "https://example.test/Path/?q=1"),
    ("http://example.test:80", "http://example.test/"),
    ("https://example.test:8443/a", "https://example.test:8443/a"),
    ("  https://example.test/a/ ", "https://example.test/a/"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("bad", ["", "ftp://x.test/", "javascript:alert(1)", "https://", "https://u:p@x.test/", "a b"])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        normalize_url(bad)


def test_same_site():
    assert same_site("www.example.test", "example.test")
    assert not same_site("evil.test", "example.test")


def test_config_validation_collects_errors():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({})
    msg = str(exc.value)
    assert "BASE_URL" in msg and "AES_KEY" in msg and "AGE_RECIPIENT" in msg


def test_config_defaults_and_secrets_not_in_repr():
    cfg = Config.from_env(make_env(COOKIE="session=SECRETVALUE"))
    assert cfg.max_workers == 2 and cfg.fetch_premium is False and cfg.max_valid_pages == 0
    assert "SECRETVALUE" not in repr(cfg) and AES_HEX not in repr(cfg)


def test_config_blank_vars_use_defaults():
    cfg = Config.from_env(make_env(MAX_WORKERS="", REQUEST_TIMEOUT="", COOKIE="", MAX_VALID_PAGES=""))
    assert cfg.max_workers == 2 and cfg.cookie is None


@pytest.mark.parametrize("name,val", [
    ("FETCH_PREMIUM", "maybe"), ("MAX_WORKERS", "0"), ("MAX_WORKERS", "x"),
    ("REQUEST_TIMEOUT", "-1"), ("BASE_URL", "ftp://x"), ("COOKIE", "a\nb"),
])
def test_config_rejects_bad_values(name, val):
    with pytest.raises(ConfigError):
        Config.from_env(make_env(**{name: val}))
