"""Deliver a completed run's artifacts as a GitHub pull request.

Everything goes through the :class:`GithubOps` facade, i.e. runs as
``github.op`` jobs inside the github-ops sandbox — the only environment
holding ``GH_TOKEN``; the credential split is preserved. Files are committed
atomically through the git data API (blobs → tree → commit → ref) rather
than per-file contents PUTs: one commit regardless of file count, and
base64 blobs carry binary content.

Scaffold status: this is a real, unit-tested code path (stubbed GithubOps),
but per the project pattern — unverified external behaviors get a seam and
an e2e check, never a confident default — it is NOT field-proven until the
real-sbx e2e workflow exercises it. Known e2e-validation items:

- TODO(e2e): branch-name collisions (re-delivering the same run id — the
  refs POST will 422; decide between force-update and suffixing)
- TODO(e2e): large workspaces (one blob POST per file; may need chunking
  or a tarball-artifact fallback beyond a few hundred files)
- TODO(e2e): executable permission bits (every file is committed 100644)
- TODO(e2e): empty repositories (no base ref to branch from)
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from sbxloop.engine.model import artifact_files
from sbxloop.errors import DeliveryError
from sbxloop.gh.ops import GithubOps, PrRef

FILE_MODE = "100644"
BODY_FILE_LIST_CAP = 50
TITLE_CLIP = 72


def branch_name(run_id: str) -> str:
    return f"sbxloop/{run_id}"


def deliver_workspace(
    ops: GithubOps,
    repo: str,
    *,
    run_id: str,
    outcome: str,
    source_dir: Path,
    base: str | None = None,
    draft: bool = False,
) -> PrRef:
    """Publish source_dir as one commit on a new branch and open a PR."""
    files = artifact_files(source_dir)
    if not files:
        raise DeliveryError(f"nothing to deliver: no files in {source_dir}")

    if base is None:
        base = str(ops.repo_get(repo).get("default_branch") or "main")
    base_sha = _base_commit_sha(ops, repo, base)
    base_tree = _commit_tree_sha(ops, repo, base_sha)

    entries = [
        {
            "path": file.relative_to(source_dir).as_posix(),
            "mode": FILE_MODE,
            "type": "blob",
            "sha": _create_blob(ops, repo, file),
        }
        for file in files
    ]
    tree = _sha(
        ops.raw("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": entries}),
        f"tree for {repo}",
    )
    commit = _sha(
        ops.raw(
            "POST",
            f"/repos/{repo}/git/commits",
            {
                "message": f"sbxloop run {run_id}: deliver artifacts\n\nOutcome: {outcome}",
                "tree": tree,
                "parents": [base_sha],
            },
        ),
        f"commit for {repo}",
    )
    branch = branch_name(run_id)
    ops.raw("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})

    return ops.pr_create(
        repo,
        base=base,
        head=branch,
        title=_title(outcome),
        body=_body(run_id, outcome, source_dir, files),
        draft=draft,
    )


def _base_commit_sha(ops: GithubOps, repo: str, base: str) -> str:
    ref = ops.raw("GET", f"/repos/{repo}/git/ref/heads/{base}")
    try:
        return str(ref["object"]["sha"])
    except (TypeError, KeyError) as exc:
        raise DeliveryError(f"cannot resolve base branch {base!r} of {repo}") from exc


def _commit_tree_sha(ops: GithubOps, repo: str, commit_sha: str) -> str:
    commit = ops.raw("GET", f"/repos/{repo}/git/commits/{commit_sha}")
    try:
        return str(commit["tree"]["sha"])
    except (TypeError, KeyError) as exc:
        raise DeliveryError(f"cannot read base commit {commit_sha} of {repo}") from exc


def _create_blob(ops: GithubOps, repo: str, file: Path) -> str:
    content = base64.b64encode(file.read_bytes()).decode("ascii")
    return _sha(
        ops.raw("POST", f"/repos/{repo}/git/blobs", {"content": content, "encoding": "base64"}),
        f"blob for {file.name}",
    )


def _sha(response: Any, what: str) -> str:
    sha = response.get("sha") if isinstance(response, dict) else None
    if not sha:
        raise DeliveryError(f"GitHub returned no sha creating {what}: {response!r}")
    return str(sha)


def _title(outcome: str) -> str:
    title = f"sbxloop: {outcome}"
    return title if len(title) <= TITLE_CLIP else title[: TITLE_CLIP - 1] + "…"


def _body(run_id: str, outcome: str, source_dir: Path, files: list[Path]) -> str:
    listed = [f"- `{f.relative_to(source_dir).as_posix()}`" for f in files[:BODY_FILE_LIST_CAP]]
    if len(files) > BODY_FILE_LIST_CAP:
        listed.append(f"- … +{len(files) - BODY_FILE_LIST_CAP} more")
    return (
        f"Artifacts produced by sbxloop run `{run_id}`.\n\n"
        f"**Outcome:** {outcome}\n\n"
        f"**Files ({len(files)}):**\n" + "\n".join(listed) + "\n"
    )
