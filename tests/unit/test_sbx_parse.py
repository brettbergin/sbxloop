"""Parser tests pinned against realistic sbx output — both the v0.35 layout
and the v0.38 one (NAME renamed to SANDBOX, frequently-empty PORTS column)."""

from sbxloop.sbx.parse import parse_columns, parse_ls, parse_version

LS_FIXTURE = """\
NAME                    AGENT    STATUS    WORKSPACE
sbxloop-r1a2b3c4d-agent   shell    running   /Users/b/.sbxloop/runs/r1a2b3c4d/workspace
sbxloop-r1a2b3c4d-github  shell    running   /Users/b/.sbxloop/runs/r1a2b3c4d/workspace
quickstart              claude   stopped   /Users/b/proj
"""

# Captured from sbx 0.38.0 on Linux (2026-08-13): NAME became SANDBOX, and
# PORTS is empty for sandboxes with no exposed ports.
LS_FIXTURE_038 = """\
SANDBOX   AGENT   STATUS    PORTS   WORKSPACE
lscheck   shell   running           /tmp/sbx-dbg
webby     shell   running   8080    /home/b/site
"""


def test_parse_ls_fixture() -> None:
    infos = parse_ls(LS_FIXTURE)
    assert [i.name for i in infos] == [
        "sbxloop-r1a2b3c4d-agent",
        "sbxloop-r1a2b3c4d-github",
        "quickstart",
    ]
    assert infos[0].agent == "shell"
    assert infos[2].status == "stopped"
    assert infos[0].workspace == "/Users/b/.sbxloop/runs/r1a2b3c4d/workspace"


def test_parse_ls_038_sandbox_header_and_empty_ports() -> None:
    infos = parse_ls(LS_FIXTURE_038)
    assert [i.name for i in infos] == ["lscheck", "webby"]
    # the empty PORTS cell must not shift WORKSPACE into the wrong column
    assert infos[0].workspace == "/tmp/sbx-dbg"
    assert infos[0].status == "running"
    assert infos[1].workspace == "/home/b/site"


def test_parse_ls_empty_and_header_only() -> None:
    assert parse_ls("") == []
    assert parse_ls("NAME  AGENT  STATUS\n") == []
    # 0.38 prints this instead of a header when the list is empty
    assert parse_ls("No sandboxes found.\n") == []


def test_parse_ls_tolerates_unknown_columns() -> None:
    text = "NAME   IMAGE           CPU\nbox1   ubuntu:24.04    2\n"
    infos = parse_ls(text)
    assert len(infos) == 1
    assert infos[0].name == "box1"
    assert infos[0].agent is None


def test_parse_columns_short_rows() -> None:
    rows = parse_columns("NAME   STATUS\nonly-name\n")
    assert rows == [{"name": "only-name", "status": ""}]


def test_parse_version() -> None:
    assert parse_version("sbx version 0.35.0\n") == "0.35.0"
    assert parse_version("v0.36.2 (nightly)") == "0.36.2"
    assert parse_version("2025/10/27 something weird") is None
