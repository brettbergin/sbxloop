"""The repositories the host's forge credential can see.

A person registering a repository from a remote client should pick it from
a list, not spell ``owner/name`` and find out at the first poll that the
token cannot see it. This reads that list from the forge with the credential
the daemon itself would use — the personal token in the host's environment
or ``secrets.env``, else the GitHub App installation — and says which of
them are declared to this daemon already.

It runs on the host, over the same credential snapshot the connection check
uses (:mod:`sbxloop.api.routes.connections`), for the same reason that
check does: the question is "what does *this* credential see", it is asked
before any repository is configured — so before the daemon has a forge
sandbox to ask through — and the answer carries no secret. A GitHub App's
installation token is minted here the way every other host-side mint is
(:mod:`sbxloop.vcs.github.appauth`) and discarded with the response.

Nothing here writes: not to the forge, not to the configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from sbxloop.api.errors import Problem
from sbxloop.api.models import AvailableRepository, DiscoveryCredential, RepositoryDiscovery
from sbxloop.config import FORGE_TOKEN_ENVS, Config, VcsKind
from sbxloop.errors import GithubOpsError, ProvisionError
from sbxloop.vcs.github import appauth

#: One page of a forge listing; GitHub's and GitLab's maximum.
PAGE_SIZE = 100
#: How many pages a listing walks before it reports itself truncated: an
#: account with thousands of repositories gets the first two thousand and
#: ``truncated``, not a request that never ends.
MAX_PAGES = 20
TIMEOUT_S = 15.0

GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")
DEFAULT_GITHUB_API_URL = "https://api.github.com"
#: Every repository a personal token can see as its own: owned, shared with
#: it as a collaborator, or in an organization it belongs to.
GITHUB_AFFILIATION = "owner,collaborator,organization_member"

Rows = list[dict[str, Any]]


def discover(
    config: Config,
    secrets: Mapping[str, str],
    forge: VcsKind,
    *,
    configured: Mapping[str, str] | None = None,
) -> RepositoryDiscovery:
    """The repositories ``forge`` shows the host's credential, sorted by
    full name, each marked with whether ``configured`` (a casefolded
    ``owner/name`` → declared spelling map, the daemon's own list) already
    names it.

    ``409 discovery_unavailable`` when there is no credential to ask with
    (or the forge has no backend); ``502 provider_error`` when the forge
    refused the credential; ``502 provider_unreachable`` when it could not
    be reached. Never a token in a message."""
    if forge == "github":
        credential, walk = _github(config, secrets)
    elif forge == "gitlab":
        credential, walk = _gitlab(config, secrets)
    else:
        raise Problem(
            409,
            "discovery_unavailable",
            f"this sbxloop version has no {forge} backend to list repositories with",
        )
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
            rows, truncated = walk(client)
    except httpx.HTTPError as exc:
        raise Problem(502, "provider_unreachable", f"could not reach the {forge} API") from exc
    known = configured or {}
    found: list[AvailableRepository] = []
    for row in rows:
        entry = _row(forge, row, known)
        if entry is not None:
            found.append(entry)
    found.sort(key=lambda entry: entry.repository.casefold())
    return RepositoryDiscovery(forge=forge, credential=credential, data=found, truncated=truncated)


def configured_repositories(config: Config) -> dict[str, str]:
    """The daemon's declared repositories, keyed for a case-insensitive
    match against what a forge lists."""
    return {entry.repo.casefold(): entry.repo for entry in config.repo_list()}


# -- GitHub ----------------------------------------------------------------------


def _github(
    config: Config, secrets: Mapping[str, str]
) -> tuple[DiscoveryCredential, Callable[[httpx.Client], tuple[Rows, bool]]]:
    api_url = config.github.api_url.rstrip("/")
    pat = next((secrets[name] for name in GH_TOKEN_ENVS if secrets.get(name)), None)
    if pat:
        # The daemon's own preference (``Provisioner.gh_credential``): a
        # personal token set beside App credentials is the one in use.
        return _github_pat(api_url, pat)
    try:
        creds = appauth.app_credentials(secrets)
    except ProvisionError as exc:
        raise Problem(409, "discovery_unavailable", str(exc)) from exc
    if creds is None:
        raise Problem(
            409,
            "discovery_unavailable",
            f"no GitHub credential on the host: set {' or '.join(GH_TOKEN_ENVS)}, or "
            "configure a GitHub App installation, before listing its repositories",
        )
    return _github_app(api_url, creds)


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_pat(
    api_url: str, token: str
) -> tuple[DiscoveryCredential, Callable[[httpx.Client], tuple[Rows, bool]]]:
    headers = _github_headers(token)
    credential = DiscoveryCredential(mode="pat")

    def walk(client: httpx.Client) -> tuple[Rows, bool]:
        user = _json(client.get(f"{api_url}/user", headers=headers), "github")
        credential.login = str(user.get("login")) if isinstance(user, dict) else None
        return _pages(
            client,
            f"{api_url}/user/repos",
            headers=headers,
            params={"affiliation": GITHUB_AFFILIATION, "sort": "full_name"},
            forge="github",
        )

    return credential, walk


def _github_app(
    api_url: str, creds: appauth.AppCredentials
) -> tuple[DiscoveryCredential, Callable[[httpx.Client], tuple[Rows, bool]]]:
    def walk(client: httpx.Client) -> tuple[Rows, bool]:
        try:
            token = appauth.mint_installation_token(creds, api_url=api_url).value
        except (GithubOpsError, ProvisionError) as exc:
            raise Problem(
                502, "provider_error", "GitHub refused to mint the App installation token"
            ) from exc
        # The installation's own list: what the App was granted, exactly.
        return _pages(
            client,
            f"{api_url}/installation/repositories",
            headers=_github_headers(token),
            params={},
            forge="github",
            key="repositories",
        )

    return DiscoveryCredential(mode="app"), walk


def _github_row(row: dict[str, Any], known: Mapping[str, str]) -> AvailableRepository | None:
    full_name = row.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    owner, name = full_name.split("/", 1)
    return AvailableRepository(
        repository=known.get(full_name.casefold(), full_name),
        forge="github",
        owner=owner,
        name=name,
        private=bool(row.get("private")),
        archived=bool(row.get("archived")),
        default_branch=_text(row.get("default_branch")),
        url=_text(row.get("html_url")),
        configured=full_name.casefold() in known,
    )


# -- GitLab ----------------------------------------------------------------------


def _gitlab(
    config: Config, secrets: Mapping[str, str]
) -> tuple[DiscoveryCredential, Callable[[httpx.Client], tuple[Rows, bool]]]:
    token_env = config.vcs.token_env or FORGE_TOKEN_ENVS["gitlab"]
    token = secrets.get(token_env)
    if not token:
        raise Problem(
            409,
            "discovery_unavailable",
            f"no GitLab credential on the host: set {token_env} before listing its projects",
        )
    if not config.vcs.api_url:
        raise Problem(
            409, "discovery_unavailable", "[vcs] api_url names the GitLab API; it is unset"
        )
    api_url = config.vcs.api_url.rstrip("/")
    headers = {"PRIVATE-TOKEN": token}
    credential = DiscoveryCredential(mode="pat")

    def walk(client: httpx.Client) -> tuple[Rows, bool]:
        user = _json(client.get(f"{api_url}/user", headers=headers), "gitlab")
        credential.login = str(user.get("username")) if isinstance(user, dict) else None
        # FIELD-UNVERIFIED against a live GitLab: the projects the token is
        # a member of, in path order, as the REST reference documents it.
        return _pages(
            client,
            f"{api_url}/projects",
            headers=headers,
            params={"membership": "true", "simple": "true", "order_by": "path", "sort": "asc"},
            forge="gitlab",
        )

    return credential, walk


def _gitlab_row(row: dict[str, Any], known: Mapping[str, str]) -> AvailableRepository | None:
    full_path = row.get("path_with_namespace")
    if not isinstance(full_path, str) or "/" not in full_path:
        return None
    namespace, name = full_path.rsplit("/", 1)
    return AvailableRepository(
        repository=known.get(full_path.casefold(), full_path),
        forge="gitlab",
        owner=namespace,
        name=name,
        private=row.get("visibility") != "public",
        archived=bool(row.get("archived")),
        default_branch=_text(row.get("default_branch")),
        url=_text(row.get("web_url")),
        configured=full_path.casefold() in known,
    )


# -- the walk ----------------------------------------------------------------------


def _row(forge: str, row: dict[str, Any], known: Mapping[str, str]) -> AvailableRepository | None:
    return _github_row(row, known) if forge == "github" else _gitlab_row(row, known)


def _pages(
    client: httpx.Client,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, str],
    forge: str,
    key: str | None = None,
) -> tuple[Rows, bool]:
    """Every entry of a paged listing, ``page=`` by ``page=`` until a short
    page; ``key`` names the list inside an envelope. Truncated, not
    endless, past :data:`MAX_PAGES`."""
    rows: Rows = []
    for page in range(1, MAX_PAGES + 1):
        response = client.get(
            url, headers=headers, params={**params, "per_page": str(PAGE_SIZE), "page": str(page)}
        )
        data = _json(response, forge)
        if key is not None:
            data = data.get(key) if isinstance(data, dict) else None
        if not isinstance(data, list):
            return rows, False
        rows.extend(entry for entry in data if isinstance(entry, dict))
        if len(data) < PAGE_SIZE:
            return rows, False
    return rows, True


def _json(response: httpx.Response, forge: str) -> Any:
    """The body of a ``200``; any other status is the forge's refusal,
    reported by number and never by body (a body could echo the request's
    credential back)."""
    if response.status_code != 200:
        raise Problem(
            502,
            "provider_error",
            f"the {forge} API refused the request (HTTP {response.status_code})",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise Problem(502, "provider_error", f"the {forge} API answered malformed JSON") from exc


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
