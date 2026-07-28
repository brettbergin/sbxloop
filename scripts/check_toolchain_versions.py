#!/usr/bin/env python3
"""Report drift between sbxloop's pinned toolchain versions and upstream.

Every entry in ``sbxloop.toolchains`` pins an exact version so runs are
reproducible. Reproducible also means frozen: nothing bumped these, and a
stale pin fails *quietly* — the agent gets an old Node or Go, the run merely
produces worse work, and no test notices. This script is the alert.

It reads ``toolchains.PINNED_RELEASES`` (the single documented location for
what is pinned) and fetches each upstream's own "current release" endpoint.
Network-only and advisory: run it from CI on a schedule, never from the test
suite. Exit 0 when everything matches, 1 when something drifted, 2 when a
check could not be performed (so a broken endpoint is distinguishable from a
genuine bump).

    uv run python scripts/check_toolchain_versions.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from sbxloop.toolchains import PINNED_RELEASES, PinnedRelease

TIMEOUT = 30.0
_UA = {"User-Agent": "sbxloop-toolchain-version-check"}


def _get(url: str) -> tuple[str, Any]:
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read().decode("utf-8", "replace")
    try:
        return raw, json.loads(raw)
    except ValueError:
        return raw, None


def _latest(release: PinnedRelease) -> str | None:
    """Upstream's current version for ``release``, or None if unreadable."""
    raw, data = _get(release.current_url)
    constant = release.constant

    if constant == "GRADLE_VERSION":
        return str(data["version"]) if data else None
    if constant == "COMPOSER_VERSION":
        return str(data["stable"][0]["version"]) if data else None
    if constant == "NODE_VERSION":
        lts = [entry for entry in (data or []) if entry.get("lts")]
        return str(lts[0]["version"]).lstrip("v") if lts else None
    if constant == "TYPESCRIPT_VERSION":
        return str(data["dist-tags"]["latest"]) if data else None
    if constant == "GO_VERSION":
        stable = [entry for entry in (data or []) if entry.get("stable")]
        return str(stable[0]["version"]).removeprefix("go") if stable else None
    if constant == "RUSTUP_VERSION":
        for line in raw.splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
        return None
    if constant == "DOTNET_SDK_VERSION":
        channels = (data or {}).get("releases-index", [])
        lts = [c for c in channels if c.get("release-type") == "lts"]
        return str(lts[0]["latest-sdk"]) if lts else None
    if constant == "JAVA_JDK_MAJOR":
        # endoflife.date lists newest first; the pin is a major only.
        return str((data or [{}])[0].get("cycle", "")) or None
    return None


def main() -> int:
    drifted: list[tuple[PinnedRelease, str]] = []
    unchecked: list[tuple[PinnedRelease, str]] = []

    for release in PINNED_RELEASES:
        try:
            latest = _latest(release)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            unchecked.append((release, f"{type(exc).__name__}: {exc}"))
            continue
        if latest is None:
            unchecked.append((release, "could not extract a version"))
        elif latest != release.version:
            drifted.append((release, latest))
        else:
            print(f"ok       {release.constant:20} {release.version}")

    for release, latest in drifted:
        print(
            f"DRIFT    {release.constant:20} pinned {release.version} -> upstream {latest}\n"
            f"         language: {release.language}\n"
            f"         digest:   {release.digest_note}",
            file=sys.stderr,
        )
    for release, why in unchecked:
        print(f"UNKNOWN  {release.constant:20} {why}", file=sys.stderr)

    if drifted:
        print(
            f"\n{len(drifted)} pinned toolchain version(s) behind upstream. "
            "Bump the constant AND its digest together — a version bumped "
            "without its digest fails the install's checksum and silently "
            "leaves the agent to bootstrap the toolchain itself.",
            file=sys.stderr,
        )
        return 1
    if unchecked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
