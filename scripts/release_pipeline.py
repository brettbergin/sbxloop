"""Repository release/deploy automation; stdlib-only for older installed hosts.

This is workflow tooling, not part of the daemon. The deploy workflow fetches
this file at its own trusted workflow SHA, never from a PR or a release asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

QUIET_SECONDS = 180
MAX_BATCH_SECONDS = 1800
DEPLOY_COOLDOWN_SECONDS = 1800
MANIFEST = "release-manifest.json"


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value
    ):
        raise ValueError("expected a stable X.Y.Z version")
    return tuple(map(int, value.split(".")))


def commit_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("expected a full commit SHA")
    return value


def distribution_names(version: str) -> list[str]:
    version_key(version)
    return [
        f"{name}-{version}{suffix}"
        for name in ("sbxloop", "sbxloop_worker")
        for suffix in ("-py3-none-any.whl", ".tar.gz")
    ]


def command(*args: str) -> str:
    return subprocess.run(
        args, check=True, text=True, encoding="utf-8", stdout=subprocess.PIPE, timeout=120
    ).stdout.strip()


class Github:
    def __init__(self, repo: str, request: Callable[[str], Any] | None = None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("expected owner/repository")
        self.repo = repo
        self.request = request or (lambda path: json.loads(command("gh", "api", path)))

    def get(self, path: str) -> Any:
        return self.request(f"repos/{self.repo}/{path}")

    def pages(self, path: str) -> list[dict]:
        result = []
        for page in range(1, 1001):
            items = self.get(f"{path}?per_page=100&page={page}")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ValueError(f"invalid {path} response")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError(f"pagination limit reached for {path}")

    def head(self) -> str:
        return commit_sha(self.get("git/ref/heads/main")["object"]["sha"])

    def releases(self) -> dict[str, dict]:
        return {item["tag_name"]: item for item in self.pages("releases")}

    def latest(self, pinned: str = "") -> dict:
        releases = self.releases()
        if pinned:
            version_key(pinned)
            selected = releases.get(f"v{pinned}")
            if selected is None:
                raise ValueError(f"no published release for {pinned}")
        else:
            candidates = []
            for tag, item in releases.items():
                if item.get("draft") is not False or item.get("prerelease") is not False:
                    continue
                if re.fullmatch(r"v\d+\.\d+\.\d+", tag):
                    candidates.append(item)
            if not candidates:
                raise ValueError("no published stable release")
            selected = max(candidates, key=lambda item: version_key(item["tag_name"][1:]))
        validate_release(selected)
        tag = selected["tag_name"]
        matching = [entry for entry in self.pages("tags") if entry.get("name") == tag]
        if len(matching) != 1:
            raise ValueError("published release has no unambiguous tag")
        sha = commit_sha(matching[0]["commit"]["sha"])
        head = self.head()
        if sha != head and self.get(f"compare/{sha}...{head}").get("status") != "ahead":
            raise ValueError("published release is not on main")
        return {**selected, "commit_sha": sha}

    def download(self, version: str, directory: Path, names: list[str]) -> None:
        version_key(version)
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            command(
                "gh",
                "release",
                "download",
                f"v{version}",
                "--repo",
                self.repo,
                "--dir",
                str(directory),
                "--pattern",
                name,
                "--clobber",
            )


def validate_release(item: dict) -> None:
    version = item["tag_name"].removeprefix("v")
    expected = set(distribution_names(version))
    if (
        item.get("draft") is not False
        or item.get("prerelease") is not False
        or not item.get("published_at")
    ):
        raise ValueError("release is not published and stable")
    assets = item.get("assets", [])
    available = {
        asset["name"]
        for asset in assets
        if asset.get("state") == "uploaded"
        and isinstance(asset.get("size"), int)
        and asset["size"] > 0
    }
    if not expected <= available:
        raise ValueError(f"release assets incomplete for {version}")


def release_plan(api: Github, sha: str) -> dict:
    tags = [tag for tag in api.pages("tags") if re.fullmatch(r"v\d+\.\d+\.\d+", tag["name"])]
    if not tags:
        return {"action": "new", "version": "0.1.0", "sha": sha}
    latest = max(tags, key=lambda tag: version_key(tag["name"][1:]))
    version = latest["name"][1:]
    reserved_sha = commit_sha(latest["commit"]["sha"])
    if sha != reserved_sha and api.get(f"compare/{reserved_sha}...{sha}").get("status") != "ahead":
        raise ValueError("latest version is not an ancestor of the selected main commit")
    released = api.releases().get(latest["name"])
    if released is None or released.get("draft") is True:
        return {"action": "resume", "version": version, "sha": reserved_sha}
    validate_release(released)
    if sha == reserved_sha:
        return {"action": "noop", "version": version, "sha": sha}
    major, minor, patch = version_key(version)
    return {"action": "new", "version": f"{major}.{minor}.{patch + 1}", "sha": sha}


def batch(api: Github, *, manual: bool, clock=time.monotonic, sleep=time.sleep) -> dict:
    sha = api.head()
    initial = release_plan(api, sha)
    if manual or initial["action"] != "new":
        print(
            f"Release batch: {initial['action']} {initial['version']} at {initial['sha']}",
            flush=True,
        )
        return initial
    print(
        f"Waiting for {QUIET_SECONDS}s of quiet, at most {MAX_BATCH_SECONDS}s; main={sha}",
        flush=True,
    )
    start = changed = clock()
    while clock() - changed < QUIET_SECONDS and clock() - start < MAX_BATCH_SECONDS:
        sleep(15)
        current = api.head()
        if current != sha:
            sha, changed = current, clock()
            print(
                f"Main advanced to {sha}; quiet window reset, maximum deadline unchanged",
                flush=True,
            )
    # A tag made by an operator during the wait takes precedence. The selected
    # SHA is frozen here; testing and building must never check out main again.
    plan = release_plan(api, sha)
    print(f"Frozen release batch: {plan['action']} {plan['version']} at {plan['sha']}", flush=True)
    return plan


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def output(**values: Any) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if isinstance(value, bool):
                value = str(value).lower()
            if "\n" in str(value) or "\r" in str(value):
                raise ValueError("multiline workflow output refused")
            stream.write(f"{key}={value}\n")


def manifest_for(plan: dict, dist: Path) -> dict:
    return {
        "schema": 1,
        "version": plan["version"],
        "sha": commit_sha(plan["sha"]),
        "files": {
            name: hashlib.sha256((dist / name).read_bytes()).hexdigest()
            for name in distribution_names(plan["version"])
        },
    }


def verify_manifest(plan: dict, dist: Path) -> None:
    expected = json.loads((dist / MANIFEST).read_text())
    if expected != manifest_for(plan, dist):
        raise ValueError("release manifest does not match the original package files and commit")


def restore_staged(api: Github, plan: dict, dist: Path) -> bool:
    item = api.releases().get(f"v{plan['version']}")
    if item is None:
        return False
    if item.get("draft") is not True:
        raise ValueError("reserved release was already published; start a new batch check")
    if not any(asset["name"] == MANIFEST for asset in item.get("assets", [])):
        # Publication cannot start until this marker was uploaded LAST. Any
        # interrupted staging can be rebuilt; staged bytes may never be rebuilt.
        return False
    api.download(plan["version"], dist, [*distribution_names(plan["version"]), MANIFEST])
    verify_manifest(plan, dist)
    return True


def stage(api: Github, plan: dict, dist: Path) -> None:
    version = plan["version"]
    item = api.releases().get(f"v{version}")
    if item and item.get("draft") is not True:
        raise ValueError("refusing to replace published release assets")
    if item and any(asset["name"] == MANIFEST for asset in item.get("assets", [])):
        verify_manifest(plan, dist)
        return
    if item is None:
        command(
            "gh",
            "release",
            "create",
            f"v{version}",
            "--repo",
            api.repo,
            "--verify-tag",
            "--draft",
            "--title",
            f"v{version}",
            "--generate-notes",
        )
    manifest = manifest_for(plan, dist)
    command(
        "gh",
        "release",
        "upload",
        f"v{version}",
        "--repo",
        api.repo,
        "--clobber",
        *(str(dist / name) for name in distribution_names(version)),
    )
    write_json(dist / MANIFEST, manifest)
    command("gh", "release", "upload", f"v{version}", "--repo", api.repo, str(dist / MANIFEST))


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or set(state) != {
        "finished_at",
        "blocked_through",
        "in_progress",
    }:
        raise ValueError("invalid deployment state; inspect it before retrying")
    stamp = state["finished_at"]
    if type(stamp) is not int or stamp < 0 or type(state["in_progress"]) is not bool:
        raise ValueError("invalid deployment timestamp or outcome")
    if not isinstance(state["blocked_through"], str):
        raise ValueError("invalid blocked deployment version")
    if state["blocked_through"]:
        version_key(state["blocked_through"])
    return state


def deploy_decision(current: str, target: str, state: dict, *, now: int, manual: bool) -> str:
    current_key, target_key = version_key(current), version_key(target)
    if state.get("in_progress") and not manual:
        raise ValueError(
            "previous deployment was interrupted or could not restore health; recover manually"
        )
    if state.get("in_progress") and manual:
        return "deploy"
    if current_key == target_key:
        return "current"
    if manual:
        return "deploy"
    if target_key < current_key:
        return "older"
    if state.get("blocked_through") and target_key <= version_key(state["blocked_through"]):
        return "blocked"
    if "finished_at" in state and now - state["finished_at"] < DEPLOY_COOLDOWN_SECONDS:
        return "cooldown"
    return "deploy"


def receipt_valid(data: dict) -> bool:
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or type(data.get("published")) is not bool
    ):
        raise ValueError("missing or invalid release result")
    version_key(data["version"])
    commit_sha(data["sha"])
    return data["published"]


def main() -> None:
    mode = sys.argv[1]
    api = Github(os.environ["GITHUB_REPOSITORY"])
    manual = os.environ.get("MANUAL") == "true"
    if mode == "batch":
        plan = batch(api, manual=manual)
        write_json(Path("pipeline/plan.json"), plan)
        output(**plan, proceed=plan["action"] != "noop")
        return
    if mode in {"restore", "stage", "publish"}:
        plan = json.loads(Path(os.environ["PLAN"]).read_text())
        version_key(plan["version"])
        if commit_sha(plan["sha"]) != command("git", "rev-parse", "HEAD"):
            raise ValueError("checkout differs from the tested release commit")
        dist = Path("dist")
        if mode == "restore":
            output(reused=restore_staged(api, plan, dist))
        elif mode == "stage":
            stage(api, plan, dist)
            # The manifest belongs on GitHub, not on PyPI.
            (dist / MANIFEST).unlink()
        else:
            for attestation in sorted(dist.glob("*.publish.attestation")):
                command(
                    "gh",
                    "release",
                    "upload",
                    f"v{plan['version']}",
                    "--repo",
                    api.repo,
                    "--clobber",
                    str(attestation),
                )
            command(
                "gh", "release", "edit", f"v{plan['version']}", "--repo", api.repo, "--draft=false"
            )
            validate_release(api.latest(plan["version"]))
            output(published=True)
        return
    if mode == "receipt":
        plan = json.loads(Path(os.environ["PLAN"]).read_text())
        data = {
            "schema": 1,
            "published": os.environ.get("PUBLISHED") == "true",
            "version": plan["version"],
            "sha": plan["sha"],
        }
        receipt_valid(data)
        write_json(Path("release-result.json"), data)
        return
    if mode == "read-receipt":
        data = json.loads(Path(os.environ["RECEIPT"]).read_text())
        output(eligible=receipt_valid(data))
        return
    state_path = Path(os.environ["SBXLOOP_HOME"]) / "state" / "deploy.json"
    state = read_state(state_path)
    if mode == "deploy-check":
        target = api.latest(os.environ.get("INPUT_VERSION", "").removeprefix("v"))["tag_name"][1:]
        current = command(os.environ["VENV_SBXLOOP"], "--version").split()[1]
        decision = deploy_decision(current, target, state, now=int(time.time()), manual=manual)
        print(f"Deployment decision: {decision}; installed {current}; selected {target}")
        output(current=current, version=target, changed=decision == "deploy", reason=decision)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as stream:
                stream.write(f"Deployment: {decision}; installed {current}; selected {target}.\n")
    elif mode == "deploy-start":
        target = os.environ["VERSION"]
        version_key(target)
        floor = max((state.get("blocked_through") or target, target), key=version_key)
        if manual:
            floor = max((floor, api.latest()["tag_name"][1:]), key=version_key)
        write_json(
            state_path,
            {
                "finished_at": state.get("finished_at", 0),
                "blocked_through": floor,
                "in_progress": True,
            },
        )
    elif mode == "deploy-finish":
        state["finished_at"] = int(time.time())
        state["in_progress"] = not (
            os.environ.get("HEALTH") == "success" or os.environ.get("RESTORED") == "true"
        )
        write_json(state_path, state)
    elif mode == "verify-download":
        version = os.environ["VERSION"]
        item = api.latest(version)
        dist = Path(os.environ["DIST"])
        if any(asset["name"] == MANIFEST for asset in item["assets"]):
            api.download(version, dist, [MANIFEST])
            manifest = json.loads((dist / MANIFEST).read_text())
            if manifest.get("schema") != 1 or manifest.get("version") != version:
                raise ValueError("invalid release manifest")
            commit_sha(manifest["sha"])
            if manifest["sha"] != item["commit_sha"]:
                raise ValueError("release manifest commit differs from its tag")
            for name in distribution_names(version):
                if (
                    name.endswith(".whl")
                    and hashlib.sha256((dist / name).read_bytes()).hexdigest()
                    != manifest["files"][name]
                ):
                    raise ValueError("downloaded wheel differs from the published release")
    else:
        raise ValueError(f"unknown command: {mode}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::release pipeline: {error}", file=sys.stderr)
        sys.exit(1)
