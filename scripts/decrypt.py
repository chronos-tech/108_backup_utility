#!/usr/bin/env python3
"""Decrypt ONE cached page locally.

    AES_KEY=... python scripts/decrypt.py --identity key.txt --url https://example.com/post/ > page.html

The URL is only used to compute the SHA-256 filename; nothing is listed or
written except the page you asked for (to stdout).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyrage  # noqa: E402
from pyrage import x25519  # noqa: E402

from src import crypto  # noqa: E402
from src.utils import normalize_url  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", required=True, help="file with the age private key")
    ap.add_argument("--url", help="page URL")
    ap.add_argument("--sha", help="SHA-256 filename stem (alternative to --url)")
    args = ap.parse_args()
    if bool(args.url) == bool(args.sha):
        ap.error("give exactly one of --url or --sha")
    sha = args.sha or crypto.url_sha256(normalize_url(args.url))
    path = Path(__file__).resolve().parent.parent / crypto.cache_filename(sha)
    identity = None
    for line in Path(args.identity).read_text().splitlines():
        if line.startswith("AGE-SECRET-KEY-"):
            identity = x25519.Identity.from_str(line.strip())
    if identity is None:
        print("no age identity found", file=sys.stderr)
        return 1
    try:
        sys.stdout.buffer.write(pyrage.decrypt(path.read_bytes(), [identity]))
    except (FileNotFoundError, pyrage.DecryptError):
        print("cannot decrypt (file missing or wrong identity)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
