"""Identifier generation for runs, jobs, and tasks."""

from __future__ import annotations

import re
import secrets

# Crockford-flavored base32: lowercase, no i/l/o/u lookalikes.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

_RUN_ID_RE = re.compile(r"^r[0-9abcdefghjkmnpqrstvwxyz]{8}$")


def _token(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_run_id() -> str:
    """A short, sandbox-name-safe run id, e.g. ``r7k2m9qp3``-style."""
    return "r" + _token(8)


def branch_name(run_id: str) -> str:
    """The run's git branch, shared by workspace clones and PR delivery."""
    return f"sbxloop/{run_id}"


def is_run_id(value: str) -> bool:
    return _RUN_ID_RE.fullmatch(value) is not None


def new_job_id() -> str:
    return "j" + _token(10)


def new_message_id() -> str:
    """Id for one interactive chat message posted into a run."""
    return "m" + _token(10)


def task_id(index: int) -> str:
    """Deterministic task ids within a run: t1, t2, ..."""
    if index < 1:
        raise ValueError("task index starts at 1")
    return f"t{index}"
