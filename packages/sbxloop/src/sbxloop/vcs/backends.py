"""Which backend answers a forge kind, and how one is built (#1017).

``[vcs] kind`` names a forge; this module turns the name into a backend
object over a worker client, carrying the transport descriptor (#1015)
that tells the worker how the forge is spoken to. The engine's run box
and the daemon's long-lived box both come through :func:`backend_for`;
``sbxloop doctor`` reads :func:`capabilities_for` and
:func:`unimplemented_roles` to say what a kind can do before a run
starts.

A kind the configuration names but no backend answers
(:class:`BackendNotImplemented`) fails closed here, at the first
attempt to build one — never as a wrong-forge request later.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from sbxloop.config import FORGE_TOKEN_ENVS
from sbxloop.errors import GithubOpsError
from sbxloop.vcs.github.ops import GithubOps, github_transport
from sbxloop.vcs.gitlab.ops import (
    SANDBOX_TOKEN_ENV as GITLAB_SANDBOX_TOKEN_ENV,
    GitlabOps,
    gitlab_transport,
)
from sbxloop.vcs.protocol import Capability, VcsOps
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import TransportSpec


class BackendNotImplemented(GithubOpsError):
    """``[vcs] kind`` names a forge no backend answers yet."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            f'[vcs] kind = "{kind}" names a backend that is not implemented yet; '
            f"the backends that answer a run today are {', '.join(sorted(BACKENDS))}"
        )
        self.kind = kind


class BackendClass(Protocol):
    """What the registry knows about a backend class without building one."""

    CAPABILITIES: Mapping[str, Capability]
    UNIMPLEMENTED_ROLES: tuple[str, ...]
    CAPABILITY_NOTE: str


#: The backend class per kind. A kind absent here loads in the
#: configuration and fails at the factory.
BACKENDS: dict[str, type[Any]] = {"github": GithubOps, "gitlab": GitlabOps}

#: The variable each kind's sandbox holds its token in (#1029): GitHub's
#: pair, GitLab's one name. Names travel; values never do.
SANDBOX_TOKEN_ENVS: dict[str, tuple[str, ...]] = {
    "github": ("GH_TOKEN", "GITHUB_TOKEN"),
    "gitlab": (GITLAB_SANDBOX_TOKEN_ENV,),
    "gitea": (FORGE_TOKEN_ENVS["gitea"],),
}


def sandbox_token_envs(kind: str) -> tuple[str, ...]:
    """The variable(s) a github-role box provisioned for ``kind`` holds
    the forge token in."""
    return SANDBOX_TOKEN_ENVS.get(kind, ("GH_TOKEN", "GITHUB_TOKEN"))


def transport_for(kind: str, api_url: str | None) -> TransportSpec | None:
    """The transport descriptor for ``kind`` at ``api_url``. GitHub's
    descriptor is optional (a job without one is served as GitHub from
    the sandbox's environment); another forge must name its API root."""
    if kind == "github":
        return github_transport(api_url) if api_url else None
    if api_url is None:
        raise GithubOpsError(
            f"[vcs] api_url is not set: a {kind} backend needs the API root it speaks to "
            f'(for example "https://{kind}.example.com/api/v4")'
        )
    if kind == "gitlab":
        return gitlab_transport(api_url)
    raise BackendNotImplemented(kind)


def backend_for(
    kind: str,
    client: WorkerClient,
    run_id: str,
    *,
    api_url: str | None,
    timeout_s: float = 120.0,
) -> VcsOps:
    """A backend of ``kind`` over ``client``, carrying the descriptor for
    ``api_url``."""
    cls = BACKENDS.get(kind)
    if cls is None:
        raise BackendNotImplemented(kind)
    transport = transport_for(kind, api_url)
    backend: VcsOps = cls(client, run_id, timeout_s=timeout_s, transport=transport)
    return backend


def capabilities_for(kind: str) -> Mapping[str, Capability] | None:
    """The static capability report of ``kind``'s backend class, or
    ``None`` when no backend answers the kind."""
    cls = BACKENDS.get(kind)
    return dict(cls.CAPABILITIES) if cls is not None else None


def unimplemented_roles(kind: str) -> tuple[str, ...]:
    """The roles ``kind``'s backend raises
    :class:`~sbxloop.errors.RoleNotImplemented` for; empty for a complete
    backend and for a kind with none."""
    cls = BACKENDS.get(kind)
    return tuple(getattr(cls, "UNIMPLEMENTED_ROLES", ())) if cls is not None else ()


def capability_note(kind: str) -> str:
    """One sentence the doctor prints beside an ``UNKNOWN``."""
    cls = BACKENDS.get(kind)
    return str(getattr(cls, "CAPABILITY_NOTE", "")) if cls is not None else ""
