#!/usr/bin/env python3
"""Generate an AES-256 key and an age X25519 key pair for LOCAL use.

Prints secrets to the terminal only. Store the AES key as the GitHub Secret
AES_KEY, the age public key as the Variable AGE_RECIPIENT, and keep the age
identity (private key) offline. Never commit any of them.
"""
import base64
import secrets

from pyrage import x25519

identity = x25519.Identity.generate()
print("AES_KEY (secret, 64 hex chars):")
print(secrets.token_hex(32))
print()
print("AGE_RECIPIENT (public, GitHub Variable):")
print(identity.to_public())
print()
print("age IDENTITY (PRIVATE - keep offline, never upload to GitHub):")
print(identity)
