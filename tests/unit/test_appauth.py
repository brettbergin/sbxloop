"""GitHub App auth (#568): credential resolution, JWT signing, minting, refresh."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from sbxloop.errors import GithubOpsError, ProvisionError
from sbxloop.gh.appauth import (
    APP_ID_ENV,
    APP_INSTALLATION_ID_ENV,
    APP_KEY_ENV,
    APP_KEY_PATH_ENV,
    AppCredentials,
    AppTokenSource,
    InstallationToken,
    app_credentials,
    app_jwt,
    fetch_app_slug,
    mint_installation_token,
)

FULL_ENV = {
    APP_ID_ENV: "12345",
    APP_INSTALLATION_ID_ENV: "678",
    APP_KEY_ENV: "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
}


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway RSA key from the same openssl the code signs with."""
    path = tmp_path_factory.mktemp("keys") / "app.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _b64pad(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class TestCredentialResolution:
    def test_no_app_vars_means_none(self) -> None:
        assert app_credentials({}) is None
        assert app_credentials({"GH_TOKEN": "github_pat_x"}) is None

    def test_full_set_inline_key(self) -> None:
        creds = app_credentials(FULL_ENV)
        assert creds == AppCredentials("12345", "678", FULL_ENV[APP_KEY_ENV])

    def test_full_set_key_path(self, tmp_path: Path) -> None:
        pem = tmp_path / "app.pem"
        pem.write_text("-----BEGIN RSA PRIVATE KEY-----\nkey\n-----END RSA PRIVATE KEY-----")
        env = {
            APP_ID_ENV: "12345",
            APP_INSTALLATION_ID_ENV: "678",
            APP_KEY_PATH_ENV: str(pem),
        }
        creds = app_credentials(env)
        assert creds is not None
        assert "BEGIN RSA PRIVATE KEY" in creds.private_key_pem

    def test_partial_set_names_the_missing_pieces(self) -> None:
        with pytest.raises(ProvisionError, match="incomplete GitHub App credentials") as exc:
            app_credentials({APP_ID_ENV: "12345"})
        assert APP_INSTALLATION_ID_ENV in str(exc.value)
        assert APP_KEY_ENV in str(exc.value)

    def test_both_key_forms_refused(self, tmp_path: Path) -> None:
        pem = tmp_path / "app.pem"
        pem.write_text("-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----")
        env = {**FULL_ENV, APP_KEY_PATH_ENV: str(pem)}
        with pytest.raises(ProvisionError, match="configure exactly one"):
            app_credentials(env)

    def test_non_numeric_installation_id(self) -> None:
        env = {**FULL_ENV, APP_INSTALLATION_ID_ENV: "my-org"}
        with pytest.raises(ProvisionError, match="numeric installation id"):
            app_credentials(env)

    def test_unreadable_key_path(self, tmp_path: Path) -> None:
        env = {
            APP_ID_ENV: "12345",
            APP_INSTALLATION_ID_ENV: "678",
            APP_KEY_PATH_ENV: str(tmp_path / "missing.pem"),
        }
        with pytest.raises(ProvisionError, match="cannot read"):
            app_credentials(env)

    def test_non_pem_content_refused(self, tmp_path: Path) -> None:
        pem = tmp_path / "app.pem"
        pem.write_text("ghp_this_is_a_token_not_a_key")
        env = {
            APP_ID_ENV: "12345",
            APP_INSTALLATION_ID_ENV: "678",
            APP_KEY_PATH_ENV: str(pem),
        }
        with pytest.raises(ProvisionError, match="does not look like a PEM"):
            app_credentials(env)

    def test_repr_never_leaks_the_key(self) -> None:
        creds = app_credentials(FULL_ENV)
        assert "PRIVATE KEY" not in repr(creds)


class TestAppJwt:
    def test_jwt_signs_verifiably_with_openssl(self, rsa_key: Path, tmp_path: Path) -> None:
        creds = AppCredentials("12345", "678", rsa_key.read_text())
        token = app_jwt(creds, now=1_700_000_000.0)
        header_b64, payload_b64, sig_b64 = token.split(".")
        assert json.loads(_b64pad(header_b64)) == {"alg": "RS256", "typ": "JWT"}
        assert json.loads(_b64pad(payload_b64)) == {
            "iat": 1_700_000_000 - 60,
            "exp": 1_700_000_000 + 540,
            "iss": "12345",
        }
        pub = tmp_path / "pub.pem"
        subprocess.run(
            ["openssl", "pkey", "-in", str(rsa_key), "-pubout", "-out", str(pub)],
            check=True,
            capture_output=True,
        )
        sig = tmp_path / "sig.bin"
        sig.write_bytes(_b64pad(sig_b64))
        data = tmp_path / "signing-input"
        data.write_bytes(f"{header_b64}.{payload_b64}".encode())
        verify = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(pub),
                "-signature",
                str(sig),
                str(data),
            ],
            capture_output=True,
        )
        assert verify.returncode == 0, verify.stderr

    def test_garbage_key_is_a_clear_error(self) -> None:
        creds = AppCredentials(
            "12345", "678", "-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----"
        )
        with pytest.raises(GithubOpsError, match="could not sign"):
            app_jwt(creds)


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class TestMint:
    def creds(self, rsa_key: Path) -> AppCredentials:
        return AppCredentials("12345", "678", rsa_key.read_text())

    def test_mint_parses_token_and_expiry(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float) -> _Resp:
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["method"] = request.get_method()
            return _Resp({"token": "ghs_fresh", "expires_at": "2026-08-30T22:14:10Z"})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        token = mint_installation_token(self.creds(rsa_key), now=1000.0)
        assert token.value == "ghs_fresh"
        assert seen["url"].endswith("/app/installations/678/access_tokens")
        assert seen["method"] == "POST"
        assert str(seen["auth"]).startswith("Bearer ")
        from datetime import datetime

        assert token.expires_at == datetime.fromisoformat("2026-08-30T22:14:10+00:00").timestamp()

    def test_unparseable_expiry_falls_back_early(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout: _Resp({"token": "ghs_x", "expires_at": "soon"}),
        )
        token = mint_installation_token(self.creds(rsa_key), now=1000.0)
        assert token.expires_at == 1000.0 + 3300.0

    def test_http_error_names_the_likely_causes(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(request: Any, timeout: float) -> _Resp:
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", None, io.BytesIO(b'{"message":"bad jwt"}')
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(GithubOpsError, match="check the") as exc:
            mint_installation_token(self.creds(rsa_key))
        assert exc.value.http_status == 401

    def test_missing_token_field_is_an_error(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda request, timeout: _Resp({"expires_at": "x"})
        )
        with pytest.raises(GithubOpsError, match="no token field"):
            mint_installation_token(self.creds(rsa_key))


class TestAppTokenSource:
    def test_caches_until_margin_then_reminets(self) -> None:
        clock = {"t": 0.0}
        minted: list[float] = []

        def mint(creds: AppCredentials) -> InstallationToken:
            minted.append(clock["t"])
            return InstallationToken(f"ghs_{len(minted)}", clock["t"] + 3600.0)

        creds = AppCredentials("1", "2", "-----BEGIN PRIVATE KEY-----")
        source = AppTokenSource(creds, clock=lambda: clock["t"], mint=mint)
        assert source.refresh_due()
        assert source.current() == "ghs_1"
        assert not source.refresh_due()
        clock["t"] = 2999.0  # 601s of lifetime left: still fresh
        assert source.current() == "ghs_1"
        clock["t"] = 3001.0  # inside the 600s margin: stale
        assert source.refresh_due()
        assert source.current() == "ghs_2"
        assert minted == [0.0, 3001.0]


class TestBotLogin:
    """The App's own ``<slug>[bot]`` identity (#569 x #536): resolved on
    the host so landing can tell the loop's threads from a human's."""

    def creds(self, rsa_key: Path) -> AppCredentials:
        return AppCredentials("12345", "678", rsa_key.read_text())

    def test_fetch_app_slug_authenticates_with_the_jwt(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float) -> _Resp:
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["method"] = request.get_method()
            return _Resp({"slug": "sbxloop-app"})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert fetch_app_slug(self.creds(rsa_key), now=1000.0) == "sbxloop-app"
        assert seen["url"].endswith("/app")
        assert seen["method"] == "GET"
        assert str(seen["auth"]).startswith("Bearer ")

    def test_http_error_names_the_app(self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(request: Any, timeout: float) -> _Resp:
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", None, io.BytesIO(b"{}")
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(GithubOpsError, match="App lookup") as exc:
            fetch_app_slug(self.creds(rsa_key))
        assert exc.value.http_status == 401

    def test_a_missing_slug_field_is_an_error(
        self, rsa_key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: _Resp({"name": "x"}))
        with pytest.raises(GithubOpsError, match="no slug"):
            fetch_app_slug(self.creds(rsa_key))

    def test_bot_login_fetches_once_and_caches(self) -> None:
        fetched: list[int] = []

        def fetch(creds: AppCredentials) -> str:
            fetched.append(1)
            return "sbxloop-app"

        source = AppTokenSource(
            AppCredentials("1", "2", "-----BEGIN PRIVATE KEY-----"), fetch=fetch
        )
        assert source.bot_login() == "sbxloop-app[bot]"
        assert source.bot_login() == "sbxloop-app[bot]"
        assert fetched == [1]

    def test_a_failed_lookup_is_cached_none(self) -> None:
        """The engine has its own fallbacks; retrying a dead lookup every
        run would add latency, not identity."""
        calls: list[int] = []

        def fetch(creds: AppCredentials) -> str:
            calls.append(1)
            raise GithubOpsError("nope")

        source = AppTokenSource(
            AppCredentials("1", "2", "-----BEGIN PRIVATE KEY-----"), fetch=fetch
        )
        assert source.bot_login() is None
        assert source.bot_login() is None
        assert calls == [1]
