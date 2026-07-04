#!/usr/bin/env python3
"""Generate a VAPID keypair for Web Push, printed as Fly secrets.

Web Push authenticates the server to the browser's push service with a VAPID
(ECDSA P-256) keypair. The PUBLIC key doubles as the browser's
applicationServerKey; the PRIVATE key signs each push. Generate ONE pair, set
it once, and keep it stable — rotating it invalidates every existing device
subscription (each phone must re-enable push).

    python scripts/gen_vapid_keys.py

Copy the printed `fly secrets set …` line, then redeploy. Run locally the same
way and `export` the vars for `python app.py`. Never commit these values.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    priv_raw = key.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public = b64url(pub_raw)     # 65-byte point -> applicationServerKey
    private = b64url(priv_raw)   # 32-byte scalar -> signs the VAPID JWT

    print("VAPID keypair generated. Set these (and keep them stable):\n")
    print("# Local dev:")
    print(f'export VAPID_PUBLIC_KEY="{public}"')
    print(f'export VAPID_PRIVATE_KEY="{private}"')
    print('export VAPID_SUBJECT="mailto:you@example.com"\n')
    print("# Fly.io:")
    print(f"fly secrets set VAPID_PUBLIC_KEY='{public}' \\")
    print(f"  VAPID_PRIVATE_KEY='{private}' \\")
    print("  VAPID_SUBJECT='mailto:you@example.com'")


if __name__ == "__main__":
    main()
