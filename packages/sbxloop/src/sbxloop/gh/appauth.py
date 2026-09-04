"""GitHub App installation auth: host-minted, short-lived tokens (#568).

The alternative to a personal access token: the operator installs a GitHub
App on the repository and configures its id, installation id and private
key. The **host** — never a sandbox — signs a short-lived RS256 JWT with
the app's private key and exchanges it at
``POST /app/installations/{id}/access_tokens`` for an installation token
(``ghs_…``, ~1 hour). Only that token enters the github-ops sandbox, via
the in-VM env file; the private key stays on the host, so the credential
domains stay exactly as narrow as they are with a PAT (the sandbox holds a
scoped, expiring token instead of a personal one).

The token mint — plus a one-time ``GET /app`` slug lookup — are the only
host→GitHub calls in the whole system, and they are the credential-minting
plane, not the ops plane: every repository operation still runs inside the
github-ops sandbox. Installation tokens are attributed by GitHub to the app
(``<app-slug>[bot]``, see :func:`fetch_app_slug`), not to a person.

Signing uses the host ``openssl`` binary (RS256 is PKCS#1 v1.5 over
SHA-256, which ``openssl dgst -sha256 -sign`` produces exactly), so no
crypto dependency is added; the token exchange is a stdlib ``urllib``
POST. Tokens are cached and re-minted when less than
:data:`REFRESH_MARGIN_S` of lifetime remains — see
``Provisioner.gh_refresher`` for how a fresh token reaches a live sandbox.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from sbxloop.errors import GithubOpsError, ProvisionError
from sbxloop.log import get_logger

log = get_logger(__name__)

# The credential set, read from the host environment / .env — like the PATs,
# never from sbxloop.toml (config files hold no credentials).
APP_ID_ENV = "GITHUB_APP_ID"
APP_INSTALLATION_ID_ENV = "GITHUB_APP_INSTALLATION_ID"
APP_KEY_ENV = "GITHUB_APP_PRIVATE_KEY"  # nosec B105 - env var name, not a secret
APP_KEY_PATH_ENV = "GITHUB_APP_PRIVATE_KEY_PATH"  # nosec B105 - env var name

APP_ENV_VARS = (APP_ID_ENV, APP_INSTALLATION_ID_ENV, APP_KEY_ENV, APP_KEY_PATH_ENV)

# Re-mint when less than this much of the installation token's lifetime
# remains. GitHub grants ~1 hour; a 10 minute margin means every job starts
# with at least that much runway, which covers the longest single github op
# (a chunked blob-batch delivery) without a mid-op expiry.
REFRESH_MARGIN_S = 600.0

# App JWTs may live at most 10 minutes; sign for 9 and backdate ``iat`` 60s
# against clock drift, per GitHub's own guidance.
_JWT_LIFETIME_S = 540
_JWT_BACKDATE_S = 60

_MINT_TIMEOUT_S = 30.0
_API_URL = "https://api.github.com"

# Fallback lifetime when the mint response carries no parseable expiry:
# assume slightly under the documented hour so refresh errs early.
_DEFAULT_LIFETIME_S = 3300.0


@dataclass(frozen=True)
class AppCredentials:
    """One GitHub App installation identity, private key included."""

    app_id: str
    installation_id: str
    private_key_pem: str

    def __repr__(self) -> str:  # never leak the key through repr/logs
        return f"AppCredentials(app_id={self.app_id!r}, installation_id={self.installation_id!r})"


class InstallationToken(NamedTuple):
    value: str
    expires_at: float  # unix seconds
    # The ``permissions`` map GitHub returns with the mint (``{"contents":
    # "write", ...}``) — what this installation may do, which `sbxloop
    # doctor` checks against what a run needs (#696). ``None`` when the
    # response carried none (a fake, or an older API).
    permissions: Mapping[str, str] | None = None


def app_credentials(env: Mapping[str, str]) -> AppCredentials | None:
    """The App credential set configured in ``env``, or ``None``.

    ``None`` means "no App variable is set at all" — PAT mode territory.
    A *partial* or malformed set raises :class:`ProvisionError` naming
    exactly what is missing, instead of letting an obscure 401 happen later.
    """
    values = {name: (env.get(name) or "").strip() for name in APP_ENV_VARS}
    if not any(values.values()):
        return None
    missing = [name for name in (APP_ID_ENV, APP_INSTALLATION_ID_ENV) if not values[name]]
    key_inline, key_path = values[APP_KEY_ENV], values[APP_KEY_PATH_ENV]
    if key_inline and key_path:
        raise ProvisionError(
            f"both {APP_KEY_ENV} and {APP_KEY_PATH_ENV} are set — configure exactly one "
            "(the PEM inline, or a path to the .pem file GitHub generated)"
        )
    if not key_inline and not key_path:
        missing.append(f"{APP_KEY_ENV} or {APP_KEY_PATH_ENV}")
    if missing:
        configured = [n for n in APP_ENV_VARS if values[n]]
        raise ProvisionError(
            f"incomplete GitHub App credentials: {', '.join(configured)} is set but "
            f"{', '.join(missing)} is not — an App needs all three of app id, "
            "installation id and private key"
        )
    if not values[APP_INSTALLATION_ID_ENV].isdigit():
        raise ProvisionError(
            f"{APP_INSTALLATION_ID_ENV} must be the numeric installation id "
            f"(got {values[APP_INSTALLATION_ID_ENV]!r}); find it in the installation "
            "settings URL: https://github.com/settings/installations/<id>"
        )
    pem = key_inline
    if key_path:
        try:
            pem = Path(key_path).expanduser().read_text()
        except OSError as exc:
            raise ProvisionError(f"cannot read {APP_KEY_PATH_ENV} ({key_path}): {exc}") from exc
    if "PRIVATE KEY" not in pem:
        source = f"{APP_KEY_PATH_ENV} ({key_path})" if key_path else APP_KEY_ENV
        raise ProvisionError(
            f"{source} does not look like a PEM private key (no PRIVATE KEY block); "
            "expected the .pem file GitHub generated for the App"
        )
    return AppCredentials(
        app_id=values[APP_ID_ENV],
        installation_id=values[APP_INSTALLATION_ID_ENV],
        private_key_pem=pem,
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(creds: AppCredentials, *, now: float | None = None) -> str:
    """A short-lived RS256 JWT asserting this App's identity.

    Signed with the host ``openssl`` binary: RS256 is PKCS#1 v1.5 over
    SHA-256, which is exactly what ``openssl dgst -sha256 -sign`` emits for
    an RSA key — no crypto library needed.
    """
    issued = int(time.time() if now is None else now)
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": issued - _JWT_BACKDATE_S,
        "exp": issued + _JWT_LIFETIME_S,
        "iss": creds.app_id,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}"
        f".{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    with tempfile.NamedTemporaryFile(  # 0600 by construction
        "w", suffix=".pem", delete=False
    ) as key_file:
        key_file.write(creds.private_key_pem)
    try:
        try:
            proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell; openssl from PATH by design
                ["openssl", "dgst", "-sha256", "-sign", key_file.name],
                input=signing_input.encode("ascii"),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GithubOpsError(
                "openssl not found on PATH — GitHub App auth signs its JWTs with the "
                "host openssl binary; install openssl or switch to a PAT (GH_TOKEN)"
            ) from exc
        if proc.returncode != 0 or not proc.stdout:
            detail = proc.stderr.decode(errors="replace").strip()[:300]
            raise GithubOpsError(
                f"openssl could not sign the GitHub App JWT (exit {proc.returncode}): "
                f"{detail or 'no error output'} — is {APP_KEY_ENV}/{APP_KEY_PATH_ENV} "
                "the RSA private key GitHub generated for this App?"
            )
    finally:
        Path(key_file.name).unlink(missing_ok=True)
    return f"{signing_input}.{_b64url(proc.stdout)}"


def _parse_expires_at(raw: object, *, now: float) -> float:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return now + _DEFAULT_LIFETIME_S


def mint_installation_token(
    creds: AppCredentials,
    *,
    now: float | None = None,
    api_url: str = _API_URL,
) -> InstallationToken:
    """Exchange an App JWT for an installation token at ``api_url`` — the
    configured GitHub's REST root (``[github] api_url``, #623)."""
    started = time.time() if now is None else now
    token_jwt = app_jwt(creds, now=started)
    url = f"{api_url}/app/installations/{creds.installation_id}/access_tokens"
    if not url.startswith("https://"):
        raise GithubOpsError(f"refusing non-HTTPS GitHub API url {api_url!r}")
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sbxloop",
        },
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - https enforced above
            request, timeout=_MINT_TIMEOUT_S
        ) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise GithubOpsError(
            f"GitHub refused the App installation token mint ({exc.code}) for app "
            f"{creds.app_id} installation {creds.installation_id}: {body} — check the "
            "app id, the installation id, and that the private key belongs to this App",
            http_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GithubOpsError(
            f"could not reach {api_url} to mint the App installation token: {exc}"
        ) from exc
    value = data.get("token")
    if not isinstance(value, str) or not value:
        raise GithubOpsError(
            "GitHub's installation token response carried no token field — "
            f"got keys {sorted(data)!r}"
        )
    expires_at = _parse_expires_at(data.get("expires_at"), now=started)
    permissions = _parse_permissions(data.get("permissions"))
    log.info(
        "github.app_token_minted",
        app_id=creds.app_id,
        installation_id=creds.installation_id,
        expires_at=datetime.fromtimestamp(expires_at).isoformat(timespec="seconds"),
    )
    return InstallationToken(value=value, expires_at=expires_at, permissions=permissions)


def _parse_permissions(raw: object) -> Mapping[str, str] | None:
    """The mint response's ``permissions`` map, string values only, or
    ``None`` when the response had no usable map."""
    if not isinstance(raw, dict):
        return None
    return {str(k): v for k, v in raw.items() if isinstance(v, str)}


def fetch_app_slug(
    creds: AppCredentials,
    *,
    now: float | None = None,
    api_url: str = _API_URL,
) -> str:
    """The App's own slug, from ``GET /app`` with its JWT.

    GitHub attributes installation-token writes to ``<slug>[bot]`` — the
    login landing needs to tell the loop's own review threads from a
    human's. ``GET /user`` cannot answer for an installation token (403,
    #581); this endpoint can, and it authenticates with the same JWT the
    mint uses.
    """
    started = time.time() if now is None else now
    token_jwt = app_jwt(creds, now=started)
    url = f"{api_url}/app"
    if not url.startswith("https://"):
        raise GithubOpsError(f"refusing non-HTTPS GitHub API url {api_url!r}")
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sbxloop",
        },
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - https enforced above
            request, timeout=_MINT_TIMEOUT_S
        ) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise GithubOpsError(
            f"GitHub refused the App lookup ({exc.code}) for app {creds.app_id}: {body} "
            "— check the app id and that the private key belongs to this App",
            http_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GithubOpsError(f"could not reach {api_url} to look up the App: {exc}") from exc
    slug = data.get("slug") if isinstance(data, dict) else None
    if not isinstance(slug, str) or not slug:
        raise GithubOpsError(f"GitHub's App payload carried no slug field for app {creds.app_id}")
    return slug


class AppTokenSource:
    """Cached installation tokens, re-minted inside the refresh margin.

    Thread-safe: engine lanes and the daemon's sources may trigger a mint
    concurrently; the lock makes it one mint, and a rewrite racing another
    only repeats the same value.
    """

    def __init__(
        self,
        creds: AppCredentials,
        *,
        clock: Callable[[], float] = time.time,
        mint: Callable[[AppCredentials], InstallationToken] | None = None,
        margin_s: float = REFRESH_MARGIN_S,
        fetch: Callable[[AppCredentials], str] | None = None,
        api_url: str = _API_URL,
    ) -> None:
        self.creds = creds
        self.api_url = api_url
        self._clock = clock
        self._mint = mint or (lambda c: mint_installation_token(c, now=clock(), api_url=api_url))
        self._fetch = fetch or (lambda c: fetch_app_slug(c, now=clock(), api_url=api_url))
        self._margin_s = margin_s
        self._lock = threading.Lock()
        self._token: InstallationToken | None = None
        # The identity GitHub attributes this installation's writes to,
        # fetched once per process; ``False`` means "not asked yet".
        self._bot_login: str | None = None
        self._bot_login_known = False

    def _stale(self, token: InstallationToken | None) -> bool:
        return token is None or self._clock() >= token.expires_at - self._margin_s

    def refresh_due(self) -> bool:
        """Whether :meth:`current` would mint rather than return the cache."""
        with self._lock:
            return self._stale(self._token)

    def current(self) -> str:
        """The installation token to use right now, minting when stale."""
        with self._lock:
            if self._stale(self._token):
                self._token = self._mint(self.creds)
            token = self._token
            assert token is not None  # _stale(None) is True, so it was just minted
            return token.value

    def permissions(self) -> Mapping[str, str] | None:
        """What the installation may do, as GitHub reported it with the
        current token's mint — ``None`` when the mint carried no map.
        Mints when nothing is cached yet, so a fresh source answers too."""
        with self._lock:
            if self._stale(self._token):
                self._token = self._mint(self.creds)
            token = self._token
            assert token is not None  # as in current()
            return token.permissions

    def bot_login(self) -> str | None:
        """The login GitHub attributes this installation's writes to
        (``<slug>[bot]``), or ``None`` when the slug lookup failed.

        Fetched once per process and cached — a failure is cached too: the
        engine has its own fallbacks (the delivered PR's author), and
        retrying a dead lookup every run would add latency, not identity.
        """
        with self._lock:
            if not self._bot_login_known:
                self._bot_login_known = True
                try:
                    self._bot_login = f"{self._fetch(self.creds)}[bot]"
                except GithubOpsError as exc:
                    log.warning(
                        "github.app_slug_lookup_failed",
                        app_id=self.creds.app_id,
                        error=str(exc),
                    )
                    self._bot_login = None
            return self._bot_login
