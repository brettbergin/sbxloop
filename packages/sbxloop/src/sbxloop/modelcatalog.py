"""A backend's last successful model discovery, shared by the CLI and console.

The cache is advisory: an omitted alias remains a valid configuration value.
Only picker metadata is persisted, never raw SDK replies or credentials.

A catalog is keyed by backend and, for the openai backend, by the endpoint it
was fetched from: a listing from one endpoint is never offered for another.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sbxloop.backends import AgentBackend, backend_for
from sbxloop.cli.models import ModelRow, fetch_backend_rows
from sbxloop.config import Config
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome

MAX_CACHE_BYTES = 1 << 20
REFRESH_AFTER_S = 24 * 60 * 60
RETRY_AFTER_S = 5 * 60
QUERY_TIMEOUT_S = 30.0
log = get_logger(__name__)


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=512)
    policy_state: str | None = Field(default=None, max_length=64)

    @field_validator("id")
    @classmethod
    def slug(cls, value: str) -> str:
        if any(character.isspace() or not character.isprintable() for character in value):
            raise ValueError("model id must be a nonempty slug without whitespace")
        return value


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    backend: Literal["copilot", "claude", "codex", "openai"]
    # The `[agent.openai] base_url` the listing came from; None for the
    # vendor backends, whose host is fixed.
    endpoint: str | None = None
    fetched_at: float = Field(ge=0, allow_inf_nan=False)
    models: list[CatalogModel] = Field(min_length=1, max_length=2000)

    @field_validator("fetched_at")
    @classmethod
    def timestamp(cls, value: float) -> float:
        if value > time.time() + 300:
            raise ValueError("model catalog timestamp is in the future")
        return value

    def stale(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.fetched_at >= REFRESH_AFTER_S


def catalog_endpoint(config: Config) -> str | None:
    """What a catalog under ``config`` is keyed by beyond the backend: the
    configured endpoint for the openai backend, nothing for the rest."""
    if config.agent.backend != "openai":
        return None
    return config.openai_for().base_url


def load_catalog(
    home: SbxloopHome, backend: AgentBackend, *, endpoint: str | None = None
) -> ModelCatalog | None:
    """The cached catalog for ``backend`` fetched from ``endpoint`` — a
    catalog from another endpoint (or none) is not offered."""
    path = home.model_catalogs / f"{backend.name}.json"
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CACHE_BYTES + 1)
        if len(data) > MAX_CACHE_BYTES:
            return None
        catalog = ModelCatalog.model_validate_json(data)
        if catalog.backend != backend.name or catalog.endpoint != endpoint:
            return None
        return catalog
    except (OSError, ValueError):
        return None


def save_catalog(
    home: SbxloopHome,
    backend: AgentBackend,
    rows: Sequence[ModelRow],
    *,
    endpoint: str | None = None,
) -> ModelCatalog:
    """Atomically replace a complete successful catalogue; failures keep the old one."""
    models = {
        row.id: CatalogModel(id=row.id, name=row.name, policy_state=row.policy_state)
        for row in rows
    }
    if not models:
        raise ValueError("The backend returned no models; keeping the previous catalog.")
    catalog = ModelCatalog.model_validate(
        {
            "backend": backend.name,
            "endpoint": endpoint,
            "fetched_at": time.time(),
            "models": list(models.values()),
        }
    )
    data = catalog.model_dump_json().encode()
    if len(data) > MAX_CACHE_BYTES:
        raise ValueError("The model catalog exceeds the cache size limit.")
    home.model_catalogs.mkdir(parents=True, exist_ok=True)
    path = home.model_catalogs / f"{backend.name}.json"
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".models-", delete=False) as file:
            scratch = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        scratch.replace(path)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
    return catalog


def refresh_catalog(
    home: SbxloopHome, backend: AgentBackend, config: Config | None = None
) -> ModelCatalog:
    """Fetch and cache; ``config`` supplies the endpoint the openai backend
    lists from and keys the catalog by."""
    endpoint = catalog_endpoint(config) if config is not None else None
    rows = fetch_backend_rows(backend, timeout_s=QUERY_TIMEOUT_S, config=config)
    return save_catalog(home, backend, rows, endpoint=endpoint)


_lock = threading.Lock()
_active: set[tuple[str, str]] = set()
_retry_at: dict[tuple[str, str], float] = {}


def refresh_after_provision(config: Config) -> threading.Thread | None:
    """Warm stale/missing catalogs off the provisioning path, once per home/backend.

    Discovery uses the same host credential and optional SDK as list-models.
    A catalog failure must never delay or roll back a successfully built box.
    """
    home, backend = config.paths, backend_for(config)
    endpoint = catalog_endpoint(config)
    key = (str(home.root.resolve()), f"{backend.name}@{endpoint or ''}")
    with _lock:
        if key in _active or time.monotonic() < _retry_at.get(key, 0):
            return None
        catalog = load_catalog(home, backend, endpoint=endpoint)
        if catalog is not None and not catalog.stale():
            return None
        _active.add(key)

    def discover() -> None:
        try:
            catalog = refresh_catalog(home, backend, config)
            log.info("models.cached", backend=backend.name, count=len(catalog.models))
        except Exception as exc:
            # SDK errors can echo credentials. The interactive refresh/listing
            # gives a diagnostic; automatic logs contain only the error type.
            log.warning("models.cache_unavailable", backend=backend.name, error=type(exc).__name__)
        finally:
            with _lock:
                _active.discard(key)
                _retry_at[key] = time.monotonic() + RETRY_AFTER_S

    thread = threading.Thread(target=discover, name=f"models-{backend.name}", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with _lock:
            _active.discard(key)
    return thread
