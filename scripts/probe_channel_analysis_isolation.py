"""Run *inside* a disposable sbx to prove the file-analysis isolation profile.

The host CI job supplies one read-only input and names a sibling host file that
must remain invisible. This deliberately uses only Python's standard library.
"""

from __future__ import annotations

import http.client
import os
import ssl
import sys
from hashlib import sha256
from pathlib import Path


def _fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    if len(sys.argv) != 5:
        _fail("usage: probe_channel_analysis_isolation.py INPUT SHA256 HOST_NEIGHBOR HOST_OUTPUT")
    original = Path(sys.argv[1])
    expected_sha256 = sys.argv[2]
    neighbor, host_output = (Path(value) for value in sys.argv[3:])
    if sha256(original.read_bytes()).hexdigest() != expected_sha256:
        _fail("original bytes were unavailable or changed")
    if neighbor.exists() or host_output.exists():
        _fail("a host-only sibling path is visible inside the analyzer sandbox")

    for name in (
        "SBXLOOP_ANALYSIS_HOST_SENTINEL",
        "DOCKERHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "COPILOT_GITHUB_TOKEN",
    ):
        value = os.environ.get(name)
        if value:
            kind = "proxy placeholder" if value.startswith("sbx-cs-") else "non-placeholder"
            _fail(f"host credential/environment variable leaked into sandbox: {name} ({kind})")

    try:
        original.write_bytes(b"changed")
    except OSError:
        pass
    else:
        _fail("uploaded original was writable")
    try:
        (original.parent / "parser-output").write_text("escaped")
    except OSError:
        pass
    else:
        _fail("analyzer wrote into the read-only input mount")

    # A transparent proxy may accept the TCP handshake before evaluating a
    # policy rule. Require real upstream response content to prove egress.
    try:
        connection = http.client.HTTPSConnection(
            "example.com", timeout=10, context=ssl._create_unverified_context()
        )
        connection.request("GET", "/")
        if b"Example Domain" in connection.getresponse().read(65_536):
            _fail("analyzer received content from an external HTTPS endpoint")
    except (OSError, http.client.HTTPException):
        pass

    # A parser may write only to its disposable VM filesystem; this path is
    # intentionally absent from the host runner after the sandbox is removed.
    Path("/tmp/channel-analysis-sandbox-output").write_text("sandbox-only")
    print("analysis isolation probe passed")


if __name__ == "__main__":
    main()
