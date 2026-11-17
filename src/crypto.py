"""Cryptographic helpers.

URL identifiers
    AES-256-GCM, fresh random 96-bit nonce per encryption (non-deterministic).
    Token format (used as the JSON key)::

        "v1." + base64url_nopad( nonce[12] || ciphertext || gcm_tag[16] )

    The format version is also bound into the AAD, so a token cannot be
    re-interpreted under another scheme.

Cache filenames
    ``cache/<sha256(normalized_url)>.html.age``

HTML
    age (X25519 recipient) via ``pyrage`` (Python bindings to the Rust ``rage``
    implementation). Encryption happens fully in memory; no age CLI needed.

No primitives are implemented by hand.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re

import pyrage
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pyrage import x25519

TOKEN_PREFIX = "v1."
NONCE_LEN = 12
TAG_LEN = 16
KEY_LEN = 32
_AAD = b"wp-sitemap-cache:url:v1"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
AGE_HEADER = b"age-encryption.org/v1\n"


class CryptoError(Exception):
    """Raised for any key, encryption or decryption problem. Messages never
    contain key material, URLs or plaintext."""


# --------------------------------------------------------------------- AES key

def parse_aes_key(value: str) -> bytes:
    """Parse AES_KEY: 64 hex chars or base64/base64url of exactly 32 bytes."""
    if not isinstance(value, str) or not value.strip():
        raise CryptoError("AES_KEY is empty")
    value = value.strip()
    if _HEX64.match(value):
        raw = bytes.fromhex(value)
    else:
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CryptoError(
                "AES_KEY must be 64 hex characters or base64 of exactly 32 bytes"
            ) from exc
    if len(raw) != KEY_LEN:
        raise CryptoError("AES_KEY must decode to exactly 32 bytes (AES-256)")
    return raw


# ------------------------------------------------------------- URL encryption

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("malformed encrypted identifier") from exc


def encrypt_url(key: bytes, normalized_url: str) -> str:
    """Encrypt a URL into a JSON-key-safe token. Different on every call."""
    try:
        nonce = os.urandom(NONCE_LEN)
        ciphertext = AESGCM(key).encrypt(nonce, normalized_url.encode("utf-8"), _AAD)
    except Exception as exc:  # noqa: BLE001 - never leak details
        raise CryptoError("URL encryption failed") from exc
    return TOKEN_PREFIX + _b64e(nonce + ciphertext)


def decrypt_url(key: bytes, token: str) -> str:
    """Recover the URL from a token, or raise CryptoError."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise CryptoError("unsupported encrypted identifier format")
    blob = _b64d(token[len(TOKEN_PREFIX):])
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise CryptoError("encrypted identifier is too short")
    nonce, ciphertext = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _AAD).decode("utf-8")
    except InvalidTag as exc:
        raise CryptoError(
            "AES-GCM authentication failed (wrong AES_KEY or corrupted identifier)"
        ) from exc
    except UnicodeDecodeError as exc:
        raise CryptoError("decrypted identifier is not valid UTF-8") from exc


# ----------------------------------------------------------------- hashing

def url_sha256(normalized_url: str) -> str:
    """Lowercase hex SHA-256 of the (already normalized) URL."""
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def cache_filename(sha: str) -> str:
    """Repo-relative POSIX path of the encrypted cache file for ``sha``."""
    if not SHA_RE.match(sha):
        raise CryptoError("invalid SHA-256 identifier")
    return f"cache/{sha}.html.age"


# ----------------------------------------------------------------- age HTML

def parse_age_recipient(value: str) -> x25519.Recipient:
    if not isinstance(value, str) or not value.strip():
        raise CryptoError("AGE_RECIPIENT is empty")
    try:
        return x25519.Recipient.from_str(value.strip())
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(
            "AGE_RECIPIENT is not a valid age X25519 public key (expected 'age1...')"
        ) from exc


def encrypt_html(html: bytes, recipient: x25519.Recipient) -> bytes:
    """Encrypt page bytes to the recipient. Output is a binary age file."""
    try:
        return pyrage.encrypt(html, [recipient])
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("age encryption failed") from exc


# -------------------------------------------------------------- validation

def validate_crypto_config(
    aes_key: str, age_recipient: str | None = None
) -> tuple[bytes, x25519.Recipient | None]:
    """Parse and self-test key material. Fails closed before any data is touched."""
    key = parse_aes_key(aes_key)
    probe = "https://example.invalid/probe"
    if decrypt_url(key, encrypt_url(key, probe)) != probe:
        raise CryptoError("AES self-test failed")
    recipient = None
    if age_recipient is not None:
        recipient = parse_age_recipient(age_recipient)
        if not encrypt_html(b"probe", recipient).startswith(AGE_HEADER):
            raise CryptoError("age self-test failed")
    return key, recipient
