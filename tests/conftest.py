import json
import sys
from pathlib import Path

import pytest
from pyrage import x25519

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config  # noqa: E402
from src.fetcher import HttpClient  # noqa: E402

BASE = "https://example.test"
AES_HEX = "00" * 16 + "ff" * 16          # throwaway test key
IDENTITY = x25519.Identity.generate()      # throwaway, per test session
PAGE = ("<!doctype html><html><head><title>t</title></head><body>" + "x" * 400 + "</body></html>").encode()
PREMIUM_PAGE = PAGE.replace(b"x" * 400, b'<img src="/2014/11/login-VIP.jpg">' + b"y" * 400)


def make_env(**over):
    env = {
        "BASE_URL": BASE,
        "AES_KEY": AES_HEX,
        "AGE_RECIPIENT": str(IDENTITY.to_public()),
        "REQUEST_DELAY_MIN": "0",
        "REQUEST_DELAY_MAX": "0",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def make_cfg(**over):
    return Config.from_env(make_env(**over))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "data" / "cache.json").write_text("{}\n")
    (tmp_path / "data" / "premium.json").write_text("{}\n")
    return tmp_path


@pytest.fixture
def client_factory():
    sleeps = []

    def factory(cfg):
        client = HttpClient(cfg, sleep=sleeps.append)
        client.sleeps = sleeps
        return client

    return factory


def read_json(root, name):
    return json.loads((root / "data" / name).read_text())
