"""Where a live forge is, if one is configured.

A live forge is configured by environment variables, or by the env file
the seed scripts write when ``SBXLOOP_LIVE_ENV_FILE`` names it (the
environment wins over the file). Nothing is read from ``.state/`` unless
it is named: a machine that once ran the harness does not start reaching
for containers on every test run.

:func:`live_forge` answers a :class:`LiveForge` or the reason there is
none, which is what a test skips with.
"""

from __future__ import annotations

import os
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from tests.live._http import Client, read_env_file

ENV_FILE_VAR = "SBXLOOP_LIVE_ENV_FILE"
CA_FILE_VAR = "SBXLOOP_LIVE_CA_FILE"

# kind -> (API root variable, token variable, how the token rides)
FORGES: dict[str, tuple[str, str, str]] = {
    "gitlab": ("SBXLOOP_LIVE_GITLAB_URL", "GITLAB_TOKEN", "private-token"),
    "gitea": ("SBXLOOP_LIVE_GITEA_URL", "GITEA_TOKEN", "token"),
}


def live_values() -> dict[str, str]:
    named = os.environ.get(ENV_FILE_VAR, "").strip()
    values = read_env_file(Path(named)) if named else {}
    values.update(
        {
            k: v
            for k, v in os.environ.items()
            if k.startswith(("SBXLOOP_LIVE_", "GITLAB_", "GITEA_"))
        }
    )
    return values


def auth_header(kind: str, token: str) -> dict[str, str]:
    style = FORGES[kind][2]
    return (
        {"PRIVATE-TOKEN": token}
        if style == "private-token"
        else {"Authorization": f"token {token}"}
    )


@dataclass(frozen=True)
class LiveForge:
    kind: str
    api_url: str
    values: dict[str, str]

    def get(self, key: str) -> str:
        value = self.values.get(key, "")
        if not value:
            raise KeyError(f"{key} is not in the live environment; run the {self.kind} seed")
        return value

    @property
    def prefix(self) -> str:
        return f"SBXLOOP_LIVE_{self.kind.upper()}_"

    @property
    def repo(self) -> str:
        return self.get(self.prefix + "REPO")

    @property
    def version(self) -> str:
        return self.get(self.prefix + "VERSION")

    @property
    def ca_file(self) -> Path | None:
        named = self.values.get(CA_FILE_VAR, "")
        return Path(named) if named else None

    def client(self, token_var: str, who: str) -> Client:
        return Client(self.api_url, auth_header(self.kind, self.get(token_var)), who)


def live_forge(kind: str) -> LiveForge | str:
    """The configured live ``kind`` that answers, or why there is none."""
    url_var, token_var, _ = FORGES[kind]
    values = live_values()
    if not values.get(url_var) or not values.get(token_var):
        return f"live {kind} not configured: set {url_var} and {token_var} (or {ENV_FILE_VAR})"
    forge = LiveForge(kind, values[url_var].rstrip("/"), values)
    if forge.ca_file is not None:
        os.environ.setdefault(CA_FILE_VAR, str(forge.ca_file))
    try:
        probe = forge.client(token_var, "probe").get("/version", check=False)
    except (OSError, urllib.error.URLError) as exc:
        return f"live {kind} configured at {forge.api_url} but not answering: {exc}"
    if not probe.ok:
        return f"live {kind} at {forge.api_url} refused the token: HTTP {probe.status}"
    return forge
