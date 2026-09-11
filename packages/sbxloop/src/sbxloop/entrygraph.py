"""The entrygraph recipe: a fixed `tool` run, queued by the concierge.

Only selection and task preparation run on the host. Configured repository
inputs use the run's existing credential-isolated checkout path; public URL
inputs are cloned with GitPython inside the sandbox. The analysis is one
shell command in that sandbox — the packaged scanner under a pinned
runtime — followed by the scanner's own check of its two reports; those
reports go to chat as they are. No agent runs anywhere in it.
"""

from __future__ import annotations

import re
import shlex
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from sbxloop.config import Config
from sbxloop.data.entrygraph_scan import ENTRYGRAPH_VERSION
from sbxloop.engine.model import TaskNeeds, TaskSpec
from sbxloop.errors import ProvisionError
from sbxloop.policy import valid_pattern

# Runtime versions are part of the recipe, not customer toolchain settings.
# The analyzer version is the scanner's own constant: the scanner refuses a
# runtime that does not match it, so one pin here and a second one there
# could only ever disagree at run time, on a run that then fails.
GITPYTHON_VERSION = "3.1.59"
LANGUAGE_PACK_VERSION = "1.12.2"
# `--no-build` keeps the runtime to published wheels. The analyzer and the
# grammar pack both ship them; a source build would instead reach hosts this
# recipe does not grant, and fail deep inside a compile rather than here.
RUNTIME = (
    "uv run --no-project --no-config --no-build --python 3.13 "
    f"--with entrygraph=={ENTRYGRAPH_VERSION} "
    f"--with gitpython=={GITPYTHON_VERSION} "
    f"--with tree-sitter-language-pack=={LANGUAGE_PACK_VERSION} python"
)
SCANNER = ".entrygraph/scan.py"
OUTPUT = "entrygraph-report"
# The package index, and nothing else: the grammar pack's wheels carry their
# grammars, so the scan runtime never fetches from a code host. An operator
# whose sandbox needs more names it in `[entrygraph] extra_hosts`. These are
# late grants; the operator's policy.deny always wins.
RUNTIME_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
)


def _public_url(value: str) -> str:
    """Accept an uncredentialed HTTPS clone URL, without echoing rejected input."""
    error = "entrygraph needs an HTTPS repository URL without credentials, query or fragment"
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value) or "\\" in value:
        raise ValueError(error)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError(error) from None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not valid_pattern(host)
        or host.startswith("*.")
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (port is not None and port < 1)
    ):
        raise ValueError(error)
    path = unquote(parts.path).rstrip("/")
    if not path or any(
        not re.fullmatch(r"[A-Za-z0-9_~.-]+", segment) or segment in {".", ".."}
        for segment in path.lstrip("/").split("/")
    ):
        raise ValueError(error)
    # Reject encoded separators and multiple leading slashes, which git hosts
    # may interpret differently from a configured owner/repository selector.
    if path != parts.path.rstrip("/") or path.startswith("//"):
        raise ValueError(error)
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def resolve_targets(
    config: Config,
    *,
    repo: str | None = None,
    url: str | None = None,
    all_repos: bool = False,
) -> list[str]:
    """Resolve one selector; no selector means every enabled repository."""
    if sum((repo is not None, url is not None, all_repos)) > 1:
        raise ValueError("choose one of repo, url or all_repos")
    entries = config.github.enabled_repos()
    if repo is not None:
        match = next((r.repo for r in entries if r.repo.casefold() == repo.casefold()), None)
        if match is None:
            raise ValueError("entrygraph target is not an enabled configured repository")
        return [match]
    if url is not None:
        canonical = _public_url(url)
        for entry in config.github.repo_list():
            known = f"{config.github.web_url}/{entry.repo}"
            if canonical.removesuffix(".git").casefold() == known.casefold():
                if not entry.enabled:
                    raise ValueError("entrygraph target is not an enabled configured repository")
                return [entry.repo]
        if not config.entrygraph.allow_public_urls:
            # The scan runs an agent over whatever that repository contains;
            # an operator may keep the choice of repository to themselves.
            raise ValueError(
                "entrygraph is configured for its configured repositories only "
                "(`[entrygraph] allow_public_urls = false`); name one of those instead"
            )
        return [canonical]
    if not entries:
        raise ValueError("no enabled repositories are configured; supply an HTTPS repository url")
    return [entry.repo for entry in entries]


def _hosts(config: Config, target: str) -> list[str]:
    """The hosts the scan runtime may reach: the index, the operator's extra
    grants, and — for a public target — the code host it is cloned from."""
    host = urlsplit(target).hostname if target.startswith("https://") else None
    extra = config.entrygraph.extra_hosts
    return list(dict.fromkeys([*RUNTIME_HOSTS, *extra, *([host] if host else [])]))


def _layout(target: str | None) -> tuple[str, str, str]:
    checkout = (
        target.split("/", 1)[1] if target and not target.startswith("https://") else "repository"
    )
    scanner_dir = ".entrygraph" if checkout != ".entrygraph" else ".entrygraph-run"
    output = OUTPUT if checkout != OUTPUT else f"{OUTPUT}-result"
    return checkout, f"{scanner_dir}/scan.py", output


def tool_config(config: Config, target: str) -> Config:
    """Narrow the run's config to what the scan may read and reach.

    The repository section keeps only the target (its token, for a
    configured repository; nothing, for a public one); the sandbox gets a
    Python toolchain and none of the operator's setup, packages or env —
    the scan never runs the target's code; the artifact excludes keep the
    scanner and the checkout out of the harvest. No workload profile: a
    tool run's bounds are its task's declared needs.
    """
    public = target.startswith("https://")
    resolved = (
        resolve_targets(config, url=target) if public else resolve_targets(config, repo=target)
    )
    if resolved != [target]:
        raise ValueError("entrygraph target changed; queue it again with the configured repository")
    github = config.github.for_repo(target if not public else None, workspace=None)
    if public:
        github = github.model_copy(update={"repo": None, "repos": []})
    else:
        github = github.model_copy(
            update={
                "repos": [
                    r.model_copy(
                        update={
                            "env": {},
                            "registries": [],
                            "setup_commands": [],
                            "apt_packages": [],
                        }
                    )
                    for r in github.repos
                ]
            }
        )
    sandbox = config.sandbox.model_copy(
        update={
            "workspace": None,
            "languages": ["python"],
            "setup_commands": [],
            "apt_packages": [],
            "env": {},
            "continue_branch": None,
        }
    )
    checkout, scanner, _ = _layout(target)
    exclude = [*config.artifacts.exclude, scanner.split("/", 1)[0], ".entrygraph-index-*"]
    if public:
        # The checkout is cut by the command itself, inside the sandbox;
        # nothing of it is a result.
        exclude.append(checkout)
    return config.model_copy(
        update={
            "github": github,
            "sandbox": sandbox,
            "registries": [],
            "artifacts": config.artifacts.model_copy(
                update={"exclude": list(dict.fromkeys(exclude))}
            ),
            "keep_on_failure": False,
        }
    )


def tool_task(config: Config, target: str) -> TaskSpec:
    """The one mechanical task: the scanner command, its check, its files.

    The command is the work — the engine runs it as a shell job and never
    hands it to an agent; ``verify_commands`` is the scanner's own
    consistency check over the two reports it wrote; ``result_files`` are
    those reports, which the chat sink carries as they are. The declared
    hosts are the run's whole egress grant (`[policy] deny` still wins).
    """
    public = target.startswith("https://")
    checkout, scanner, output = _layout(target)
    command = f"{RUNTIME} {scanner} --repo {shlex.quote(checkout)} --output {output}"
    if public:
        command += f" --url {shlex.quote(_public_url(target))}"
    else:
        command += f" --source {shlex.quote(target)}"
    return TaskSpec(
        id="entrygraph",
        title=f"entrygraph on {target}",
        description=(
            "Repository overview, entrypoints and source-to-sink findings, from a "
            "pinned analyzer over the committed tree; the reports name the scanned "
            "commit and the analysis limits. Static findings are leads for review, "
            "not proof; an empty findings list is not proof that the repository is safe."
        ),
        command=command,
        # `python3`, never `python`: the sandbox provisions python3 and uv's
        # managed interpreter, and no `python` alias — the scan command gets
        # its name resolved inside `uv run`, the check runs in the raw shell.
        # The check imports the standard library only, so the sandbox's own
        # python3 is enough and no runtime is resolved for it.
        verify_commands=[f"python3 {scanner} --check --output {output}"],
        result_files=[f"{output}/report.md", f"{output}/report.json"],
        needs=TaskNeeds(hosts=_hosts(config, target), repo=None if public else target, sink="chat"),
    )


def stage_scanner(config: Config, run_id: str, target: str) -> Path:
    """Stage trusted recipe code before mounting the workload's input directory.

    The target is named, never re-derived from the config: the scanner's
    path has to be the one ``scan_task`` told the agent to run, and a config
    that narrowed differently would stage it somewhere else in silence.
    """
    workspace = config.paths.run_workspace(run_id).resolve()
    _, scanner, _ = _layout(target)
    destination = workspace / scanner
    source = files("sbxloop.data").joinpath("entrygraph_scan.py").read_bytes()
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ProvisionError("entrygraph scanner input must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != source:
        raise ProvisionError("entrygraph scanner input already exists with different content")
    destination.write_bytes(source)
    return destination
