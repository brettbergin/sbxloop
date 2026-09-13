"""Bounded cursor pagination for every collection.

A cursor is opaque to the client: base64url JSON of the sort key the last
item had plus a digest of the filters, so a cursor from one listing cannot
be replayed into another with different filters (``invalid_cursor``).
Pages carry ``data``, ``next_cursor`` and ``has_more``; no exact counts.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from sbxloop.api.errors import Problem


class Page[T](BaseModel):
    data: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def filters_digest(filters: dict[str, Any]) -> str:
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(key: dict[str, Any], filters: dict[str, Any]) -> str:
    payload = {"k": key, "f": filters_digest(filters)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, filters: dict[str, Any]) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        key = payload["k"]
        digest = payload["f"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Problem(400, "invalid_cursor", "the cursor is not one this listing issued") from exc
    if digest != filters_digest(filters) or not isinstance(key, dict):
        raise Problem(400, "invalid_cursor", "the cursor belongs to a listing with other filters")
    return key


class PageQuery(BaseModel):
    """The query parameters every collection takes."""

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
