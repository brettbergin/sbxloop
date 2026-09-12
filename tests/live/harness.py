"""Bring the live forges up and down.

``python -m tests.live.harness up`` mints a throwaway CA and a localhost
certificate under ``.state/certs`` (both forges serve HTTPS only: the
worker transport refuses a plain-http API root, and this harness does not
weaken that), writes a random GitLab root password to
``.state/compose.env``, starts the containers and waits until both report
healthy. ``down`` stops them; ``down --volumes`` forgets every seeded
object. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import ssl
import subprocess  # nosec B404 - drives docker on the operator's machine
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / ".state"
CERTS = STATE / "certs"
CA_FILE = CERTS / "ca.crt"
COMPOSE_ENV = STATE / "compose.env"
LIVE_ENV = STATE / "live.env"
COMPOSE_FILE = HERE / "docker-compose.yml"
CONTAINERS = {"gitea": "sbxloop-live-gitea", "gitlab": "sbxloop-live-gitlab"}


LEAF_EXTENSIONS = """\
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash
"""


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)  # nosec B603 B607 - fixed argv


def ensure_certs() -> Path:
    """A CA and a ``localhost`` / ``127.0.0.1`` server certificate signed by
    it, with ``openssl`` as ``tests/conftest.py`` mints its own; the CA file
    is what a client trusts. Idempotent."""
    if all((CERTS / name).is_file() for name in ("ca.crt", "server.crt", "server.key")):
        return CA_FILE
    CERTS.mkdir(parents=True, exist_ok=True)
    ca_key, key, csr = CERTS / "ca.key", CERTS / "server.key", CERTS / "server.csr"
    leaf, ext = CERTS / "leaf.crt", CERTS / "leaf.ext"
    ext.write_text(LEAF_EXTENSIONS)
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ca_key))
    _openssl(
        "req", "-x509", "-new", "-key", str(ca_key), "-sha256", "-days", "90",
        "-subj", "/CN=sbxloop live forge CA",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        "-out", str(CA_FILE),
    )  # fmt: skip
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    _openssl("req", "-new", "-key", str(key), "-subj", "/CN=localhost", "-out", str(csr))
    _openssl(
        "x509", "-req", "-in", str(csr), "-CA", str(CA_FILE), "-CAkey", str(ca_key),
        "-CAcreateserial", "-days", "89", "-sha256", "-extfile", str(ext), "-out", str(leaf),
    )  # fmt: skip
    (CERTS / "server.crt").write_bytes(leaf.read_bytes() + CA_FILE.read_bytes())
    for scratch in (csr, ext, leaf, ca_key, CERTS / "ca.srl"):
        scratch.unlink(missing_ok=True)
    return CA_FILE


def ssl_context() -> ssl.SSLContext:
    """A client context that trusts the CA ``SBXLOOP_LIVE_CA_FILE`` names,
    else the harness CA once minted, else the system's roots (a live forge
    with a real certificate)."""
    named = os.environ.get("SBXLOOP_LIVE_CA_FILE") or (str(CA_FILE) if CA_FILE.is_file() else None)
    return ssl.create_default_context(cafile=named)


def ensure_compose_env() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    if not COMPOSE_ENV.is_file():
        COMPOSE_ENV.write_text(f"GITLAB_ROOT_PASSWORD={secrets.token_urlsafe(24)}\n")


def compose(*args: str) -> None:
    command = ["docker", "compose", "--env-file", str(COMPOSE_ENV), "-f", str(COMPOSE_FILE)]
    subprocess.run([*command, *args], check=True)  # nosec B603 B607 - fixed argv, no secret


def health(service: str) -> str:
    result = subprocess.run(  # nosec B603 B607 - fixed argv
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", CONTAINERS[service]],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "absent"


def wait(services: list[str], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    pending = list(services)
    while pending and time.monotonic() < deadline:
        pending = [s for s in pending if health(s) != "healthy"]
        if pending:
            time.sleep(10)
    for service in services:
        print(f"{service}: {health(service)}")
    return not pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.live.harness")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="mint certs, start the forges, wait until healthy")
    up.add_argument("services", nargs="*", default=sorted(CONTAINERS))
    up.add_argument("--timeout", type=float, default=900.0)
    down = sub.add_parser("down", help="stop the forges")
    down.add_argument("--volumes", action="store_true", help="also delete every seeded object")
    status = sub.add_parser("status", help="report container health")
    status.add_argument("services", nargs="*", default=sorted(CONTAINERS))
    args = parser.parse_args(argv)
    if args.command == "up":
        ensure_certs()
        ensure_compose_env()
        compose("up", "-d", *args.services)
        return 0 if wait(args.services, args.timeout) else 1
    if args.command == "down":
        ensure_compose_env()
        compose("down", *(["--volumes"] if args.volumes else []))
        return 0
    for service in args.services:
        print(f"{service}: {health(service)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
