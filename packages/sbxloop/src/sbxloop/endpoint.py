"""An operator-named model endpoint, parsed once for every path that needs it.

``[agent.openai] base_url`` names where the agent sandbox's model calls go.
Two paths on the host need its *host* rather than the URL — the agent
sandbox's network allowlist and the sbx custom-secret registration the
credential is bound to — and both were written for public, dotted,
TLD-terminated domains. A served endpoint is usually none of those: a
single-label hostname on the operator's network, an IPv4 or IPv6 literal,
an explicit port. This module is the one parser both paths read, so the
shapes accepted for an operator-configured endpoint are decided in one
place and never widen what a plan may declare (``sbxloop.policy`` keeps
its own rule for plan-declared egress).

Accepted, for the operator-configured endpoint only: ``http`` or ``https``;
a dotted domain, a single-label hostname, an IPv4 literal or a bracketed
IPv6 literal; an optional explicit port; an optional path (the API root,
``/v1`` on most servers). Refused, each with the reason named: any other
scheme, userinfo (a credential in the URL would reach ``sbx`` argv and the
event log), a query or fragment, an empty host, a port outside 1-65535.

FIELD-UNVERIFIED: whether sbx 0.35's ``policy allow network`` accepts a
port, an IPv4 literal or an IPv6 literal. The allowlist entry is therefore
the bare host — a policy that cannot narrow to a port allows the host — and
a grant sbx refuses fails provisioning by name (``sbx.provision``) rather
than leaving a sandbox to fail at its first model call.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

HostKind = Literal["domain", "hostname", "ipv4", "ipv6"]

# A dotted, TLD-terminated domain — the shape every other host path accepts.
_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")
# One DNS label: a host on the operator's own network, resolver-provided.
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


@dataclass(frozen=True)
class Endpoint:
    """A parsed ``base_url``: what the client dials and what the host binds."""

    scheme: Literal["http", "https"]
    #: Lowercased; an IPv6 literal without its brackets.
    host: str
    port: int | None
    #: The API root path, without a trailing slash; empty when none.
    path: str
    kind: HostKind

    @property
    def insecure(self) -> bool:
        """Plain HTTP: a credential sent to this endpoint travels in cleartext."""
        return self.scheme == "http"

    @property
    def authority(self) -> str:
        """``host[:port]`` as it appears in a URL — brackets restored for IPv6."""
        host = f"[{self.host}]" if self.kind == "ipv6" else self.host
        return host if self.port is None else f"{host}:{self.port}"

    @property
    def url(self) -> str:
        """The normalised base URL the SDK client is given."""
        return f"{self.scheme}://{self.authority}{self.path}"

    @property
    def policy_host(self) -> str:
        """What the network allowlist and the secret binding take: the bare
        host. The port is not narrowed (see the module docstring)."""
        return self.host


def host_kind(host: str) -> HostKind | None:
    """Which accepted shape ``host`` is, or None when it is none of them."""
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "ipv4" if address.version == 4 else "ipv6"
    lowered = host.lower()
    if _DOMAIN_RE.fullmatch(lowered):
        return "domain"
    if _LABEL_RE.fullmatch(lowered):
        return "hostname"
    return None


def parse_endpoint(base_url: str) -> Endpoint:
    """Parse an operator-named endpoint, naming what is wrong when it will
    not do — the message is what a failed config load shows."""
    raw = base_url.strip()
    if not raw:
        raise ValueError("base_url is empty")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ValueError(f"base_url {raw!r} is not a URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"base_url {raw!r} must start with http:// or https:// "
            f"(got scheme {parts.scheme or '(none)'!r})"
        )
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            f"base_url {raw!r} carries a credential in the URL; put the key in the "
            "environment variable api_key_env names instead"
        )
    if parts.query or parts.fragment:
        raise ValueError(f"base_url {raw!r} must not carry a query or fragment")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"base_url {raw!r} has an invalid port: {exc}") from None
    if not hostname:
        raise ValueError(f"base_url {raw!r} names no host")
    kind = host_kind(hostname)
    if kind is None:
        raise ValueError(
            f"base_url {raw!r} names a host that is not a domain, a hostname, "
            f"an IPv4 or an IPv6 literal: {hostname!r}"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"base_url {raw!r} has a port outside 1-65535")
    path = parts.path.rstrip("/")
    if path and not path.startswith("/"):
        path = f"/{path}"
    return Endpoint(
        scheme=parts.scheme,  # type: ignore[arg-type]
        host=hostname.lower(),
        port=port,
        path=path,
        kind=kind,
    )
