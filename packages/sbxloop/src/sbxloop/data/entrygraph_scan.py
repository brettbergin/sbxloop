"""Standalone entrygraph workload runner, copied into and run inside a sandbox.

The host may import this module to inspect its constants; entrygraph and
GitPython are loaded only when a scan is requested. Verification reads the two
report artifacts and needs neither dependency nor the original checkout.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import re
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ENTRYGRAPH_VERSION = "0.1.134"
MAX_PATHS = 100
MAX_DEPTH = 25
CLONE_TIMEOUT_SECONDS = 600
SCHEMA_VERSION = 1

LIMITS = [
    "Static findings are leads for review, not proof of a vulnerability.",
    "An empty result does not establish that the repository is safe.",
    "Search covers the analyzer's source and sink catalogs; unsupported constructs can be missed.",
    "Tests, vendored/generated directories, and oversized files are excluded by the analyzer.",
    "Repository entrygraph.toml rules can extend or disable parts of the built-in catalogs.",
    "The checkout's available submodule and LFS content bounds the files analyzed.",
    f"At most {MAX_PATHS} paths are returned, with maximum depth {MAX_DEPTH}; "
    "reaching this result limit can omit further paths even without work-budget truncation.",
]


def _https_url(value: str) -> str:
    """Reject credentials, ambiguous URL syntax, and non-HTTPS transports."""
    message = "A credential-free HTTPS repository URL without query or fragment is required"
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.strip("/")
        or "\\" in value
        or port == 0
    ):
        raise ValueError(message)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _remote_identity(value: str) -> str:
    return _https_url(value).removesuffix(".git")


def _git_environment() -> dict[str, str | None]:
    # GitPython overlays env on the inherited environment; None explicitly
    # removes a variable, including arbitrary injected GIT_CONFIG_KEY_* pairs.
    environment: dict[str, str | None] = {key: None for key in os.environ if key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GCM_INTERACTIVE": "never",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    settings = {
        "credential.helper": "",
        "core.hooksPath": os.devnull,
        "protocol.allow": "never",
        "protocol.https.allow": "always",
        "http.followRedirects": "false",
    }
    environment["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings.items()):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _checkout(repo_path: Path, url: str | None) -> Any:
    repo_type = importlib.import_module("git").Repo
    if repo_path.is_symlink():
        raise ValueError("The checkout must not be a symlink")
    if not repo_path.exists():
        if url is None:
            raise ValueError("The configured checkout does not exist")
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        return repo_type.clone_from(
            url,
            repo_path,
            depth=1,
            single_branch=True,
            no_tags=True,
            env=_git_environment(),
            kill_after_timeout=CLONE_TIMEOUT_SECONDS,
        )
    if not repo_path.is_dir():
        raise ValueError("The checkout is not a directory")
    repo = repo_type(repo_path, search_parent_directories=False)
    if repo.bare or Path(repo.working_tree_dir).resolve() != repo_path.resolve():
        repo.close()
        raise ValueError("The checkout must be a repository working-tree root")
    if url is not None:
        try:
            matches = _remote_identity(repo.remotes.origin.url) == _remote_identity(url)
        except (AttributeError, ValueError):
            matches = False
        if not matches:
            repo.close()
            raise ValueError("The existing checkout origin does not match the requested repository")
    # A retry reuses the same snapshot; it never fetches, resets, or replaces an
    # unknown checkout and cannot silently move to a newer remote revision.
    return repo


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Report value is not serializable: {type(value).__name__}")


def _code(value: Any) -> str:
    text = " ".join(str(value).split())
    return "`" + html.escape(text, quote=False).replace("`", "&#96;") + "`"


def _location(symbol: dict[str, Any], line: int | None = None) -> str:
    file = symbol.get("file")
    if not file:
        return "external symbol"
    return f"{file}:{line if line is not None else symbol.get('start_line', 0)}"


def render_report(report: dict[str, Any]) -> str:
    source = report["source"]
    stats = report["stats"]
    search = report["search"]
    lines = [
        "# Entrygraph report",
        "",
        f"Repository: {_code(source['url'] or source['repository'])}",
        f"Revision: {_code(source['revision'])}",
        f"Entrygraph: {_code(report['tool']['version'])}",
        "",
        "## Overview",
        "",
        f"Files: {stats.get('files', 0)} · symbols: {stats.get('symbols', 0)} · "
        f"edges: {stats.get('edges', 0)} · entrypoints: {stats.get('entrypoints', 0)}.",
        "",
    ]
    languages = report["detection"]["languages"]
    frameworks = report["detection"]["frameworks"]
    lines.append("Languages: " + (", ".join(_code(item["name"]) for item in languages) or "none"))
    lines.append("Frameworks: " + (", ".join(_code(item["name"]) for item in frameworks) or "none"))
    lines.extend(["", "## Entrypoints", ""])
    for entrypoint in report["entrypoints"]:
        symbol = entrypoint["symbol"]
        surface = " ".join(
            str(entrypoint.get(key) or "") for key in ("http_method", "route")
        ).strip()
        details = [entrypoint["kind"], entrypoint.get("framework"), surface]
        label = " · ".join(_code(detail) for detail in details if detail)
        lines.append(f"- {label}: {_code(symbol['qname'])} at {_code(_location(symbol))}.")
        parameters = entrypoint.get("parameters", [])
        if parameters:
            names = ", ".join(
                _code(f"{parameter['name']}:{parameter['location']}") for parameter in parameters
            )
            lines.append(f"  Parameters: {names}.")
    if not report["entrypoints"]:
        lines.append("No entrypoints were detected within the analyzer's coverage.")
    lines.extend(
        [
            "",
            "## Source-to-sink paths",
            "",
            f"Search mode: {_code(search['mode'])}; maximum {MAX_PATHS} paths, depth {MAX_DEPTH}.",
        ]
    )
    if search["mode"] == "widened":
        lines.extend(["", "The search widened to speculative edges with lower confidence."])
    if search["truncated"]:
        lines.extend(["", "**Incomplete search:** the analyzer reached its work budget."])
    if len(report["paths"]) >= MAX_PATHS:
        lines.extend(["", "The result limit was reached; additional paths may exist."])
    for index, finding in enumerate(report["paths"], 1):
        verified = finding.get("taint_verified")
        verdict = (
            "confirmed data flow"
            if verified is True
            else "reachable, but no data flow observed"
            if verified is False
            else "data flow unknown"
        )
        confidence = min((edge["confidence"] for edge in finding["edges"]), default=0)
        lines.extend(
            [
                "",
                f"### Path {index}: {verdict}",
                "",
                f"Sink category: {_code(finding.get('sink_category') or 'unknown')}; "
                f"severity: {_code(finding.get('severity') or 'unknown')}; "
                f"weakest edge confidence: {_code(confidence)}.",
                "",
            ]
        )
        symbols = finding["symbols"]
        lines.append(f"- Source {_code(symbols[0]['qname'])} at {_code(_location(symbols[0]))}.")
        for edge, caller, callee in zip(finding["edges"], symbols, symbols[1:], strict=False):
            lines.append(
                f"- {_code(caller['qname'])} → {_code(callee['qname'])} "
                f"at {_code(_location(caller, edge['line']))}."
            )
            if edge.get("arg_preview"):
                lines.append(f"  Argument evidence: {_code(edge['arg_preview'])}.")
        for key in ("source_kind", "source_category", "source_channel", "source_key"):
            if finding.get(key):
                lines.append(f"- {key.replace('_', ' ').capitalize()}: {_code(finding[key])}.")
        if finding.get("may_continue"):
            lines.append("- The path may continue through excluded or dynamic edges.")
    if not report["paths"]:
        lines.extend(["", "No source-to-sink paths were returned within the search limits."])
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {limit}" for limit in report["limits"])
    lines.extend(["", "Confidence: 0 unresolved, 1 fuzzy, 2 import-resolved, 3 exact.", ""])
    return "\n".join(lines)


def _validate_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("Invalid report: expected an object")
    try:
        valid = (
            report["schema_version"] == SCHEMA_VERSION
            and report["tool"] == {"name": "entrygraph", "version": ENTRYGRAPH_VERSION}
            and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", report["source"]["revision"]))
            and isinstance(report["source"]["repository"], str)
            and (report["source"]["url"] is None or _https_url(report["source"]["url"]))
            and isinstance(report["stats"], dict)
            and all(
                type(report["stats"][key]) is int and report["stats"][key] >= 0
                for key in ("files", "symbols", "edges", "entrypoints")
            )
            and isinstance(report["detection"]["languages"], list)
            and isinstance(report["detection"]["frameworks"], list)
            and isinstance(report["entrypoints"], list)
            and isinstance(report["paths"], list)
            and len(report["paths"]) <= MAX_PATHS
            and report["search"]["mode"] in {"precise", "widened", "strict", "explicit"}
            and isinstance(report["search"]["truncated"], bool)
            and report["search"]["max_paths"] == MAX_PATHS
            and report["search"]["max_depth"] == MAX_DEPTH
            and report["search"]["source_category"] == "all"
            and report["search"]["sink_category"] == "all"
            and report["limits"] == LIMITS
        )
        if not valid:
            raise ValueError("Invalid report schema or analysis limits")
        render_report(report)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("Invalid report structure") from exc
    return report


def check_report(output: Path) -> dict[str, Any]:
    report = _validate_report(json.loads((output / "report.json").read_text(encoding="utf-8")))
    if (output / "report.md").read_text(encoding="utf-8") != render_report(report):
        raise ValueError("The Markdown report does not match the JSON report")
    return report


def _write_atomic(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".entrygraph-", delete=False
    ) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_scan(
    repo_path: Path, output: Path, *, url: str | None = None, source: str | None = None
) -> None:
    url = _https_url(url) if url is not None else None
    source = source or repo_path.name
    repo = _checkout(repo_path, url)
    try:
        revision = str(repo.head.commit.hexsha)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise ValueError("The checkout has no full source revision")
        if repo.is_dirty(untracked_files=True):
            raise ValueError("The checkout contains files outside the committed source revision")
        prior_path = output / "report.json"
        if prior_path.exists():
            prior = check_report(output)
            if prior["source"] != {
                "repository": source,
                "url": url,
                "revision": revision,
            }:
                raise ValueError(
                    "The source revision or repository differs from the completed scan"
                )
            return
        output.mkdir(parents=True, exist_ok=True)
        entrygraph = importlib.import_module("entrygraph")
        if entrygraph.__version__ != ENTRYGRAPH_VERSION:
            raise ValueError(f"The scanner requires entrygraph {ENTRYGRAPH_VERSION}")
        with (
            tempfile.TemporaryDirectory(
                prefix=".entrygraph-index-", dir=output.parent
            ) as temporary,
            entrygraph.CodeGraph.index(repo_path, db=Path(temporary) / "index.db") as graph,
        ):
            stats = graph.stats()
            detection = graph.detect()
            entrypoints = graph.entrypoints()
            paths = graph.paths(
                source_category="all",
                sink_category="all",
                max_paths=MAX_PATHS,
                max_depth=MAX_DEPTH,
            )
            report = {
                "schema_version": SCHEMA_VERSION,
                "tool": {"name": "entrygraph", "version": entrygraph.__version__},
                "source": {"repository": source, "url": url, "revision": revision},
                "stats": stats,
                "detection": detection,
                "entrypoints": entrypoints,
                "paths": list(paths),
                "search": {
                    "mode": paths.mode,
                    "truncated": paths.truncated,
                    "source_category": "all",
                    "sink_category": "all",
                    "max_paths": MAX_PATHS,
                    "max_depth": MAX_DEPTH,
                },
                "limits": LIMITS,
            }
        serialized = json.dumps(report, default=_json_default, ensure_ascii=False, indent=2) + "\n"
        normalized = _validate_report(json.loads(serialized))
        _write_atomic(output / "report.md", render_report(normalized))
        # Commit the source pin last. A retry after a partial write can regenerate
        # artifacts; a completed scan cannot drift to a newer source revision.
        _write_atomic(output / "report.json", serialized)
        check_report(output)
    finally:
        repo.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--source", help="configured repository identifier for the report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        if args.repo is not None or args.url is not None or args.source is not None:
            parser.error("--check accepts only --output")
        check_report(args.output)
    else:
        if args.repo is None:
            parser.error("--repo is required for a scan")
        run_scan(args.repo, args.output, url=args.url, source=args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
