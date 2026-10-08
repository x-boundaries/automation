from __future__ import annotations

import hashlib


def synthetic_pdf(seed: bytes = b"synthetic invoice") -> bytes:
    body = b"%PDF-1.4\n% synthetic only\n" + seed + b"\n%%EOF\n"
    return body


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
