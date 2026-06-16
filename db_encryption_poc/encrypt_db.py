#!/usr/bin/env python3
"""Encrypt (or decrypt) a SQLite DB file for the decrypt_fs.so PoC.

Scheme MUST match decrypt_fs.c exactly:
  AES-256-CTR over the whole file, 128-bit counter starting at 0 for byte
  offset 0 and incrementing every 16 bytes (big-endian). Because AES-CTR is a
  symmetric XOR-with-keystream, the same operation encrypts and decrypts, and
  any 16-byte-aligned region can be processed independently — which is what lets
  the shim decrypt individual SQLite pages on demand.

PoC ONLY: the key is passed on the command line / env for demonstration. In
production the key must never be written anywhere the agent can read it.

Usage:
  python encrypt_db.py encrypt --in plain.db --out enc.db --key <64 hex chars>
  python encrypt_db.py decrypt --in enc.db --out plain.db --key <64 hex chars>
  python encrypt_db.py genkey
"""

import argparse
import os
import sys

KEY_BYTES = 32  # AES-256


def _transform(data: bytes, key: bytes) -> bytes:
    # Imported lazily so `genkey` works without the dependency installed.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    # initial counter = 0 (16 zero bytes), matching counter_block(0) in the shim
    cipher = Cipher(algorithms.AES(key), modes.CTR(b"\x00" * 16))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()


def _load_key(hex_str: str) -> bytes:
    key = bytes.fromhex(hex_str)
    if len(key) != KEY_BYTES:
        sys.exit(f"key must be {KEY_BYTES} bytes ({KEY_BYTES * 2} hex chars)")
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("encrypt", "decrypt"):  # identical operation for CTR
        p = sub.add_parser(name)
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", dest="out", required=True)
        p.add_argument("--key", default=os.environ.get("DB_ENC_KEY", ""))

    sub.add_parser("genkey")

    args = ap.parse_args()

    if args.cmd == "genkey":
        print(os.urandom(KEY_BYTES).hex())
        return 0

    if not args.key:
        sys.exit("provide --key or set DB_ENC_KEY")
    key = _load_key(args.key)

    with open(args.inp, "rb") as f:
        data = f.read()
    out = _transform(data, key)
    with open(args.out, "wb") as f:
        f.write(out)

    print(f"{args.cmd}ed {len(data)} bytes: {args.inp} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
