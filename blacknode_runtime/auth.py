"""Pairing-token loading and constant-time bearer authentication."""

from __future__ import annotations

import hmac
from pathlib import Path


def load_auth_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"could not read pairing token file: {path}") from exc
    if len(token) < 32:
        raise RuntimeError("pairing token is missing or invalid")
    return token


def authorization_matches(header: str | None, token: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), token)
