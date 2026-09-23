"""Sandbox names identify their owner and their purpose."""

from pathlib import Path

from sbxloop.paths import SbxloopHome
from sbxloop.sbx.naming import (
    concierge_name,
    daemon_vcs_name,
    run_name,
    run_name_candidates,
)


def test_names_show_owner_and_purpose(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "home")
    agent = run_name(home, "r7k2m9qp3", "agent")
    prefix = agent.removesuffix("-r7k2m9qp3-run-agent")
    assert prefix.startswith("sbxl-")
    assert len(prefix.removeprefix("sbxl-")) == 8
    assert run_name(home, "r7k2m9qp3", "github", vcs_kind="gitlab") == (
        f"{prefix}-r7k2m9qp3-run-vcs-gitlab"
    )
    assert run_name(home, "r7k2m9qp3", "service") == (f"{prefix}-r7k2m9qp3-run-credential-service")
    assert daemon_vcs_name(home, "gitlab") == f"{prefix}-daemon-vcs-gitlab"
    assert concierge_name(home) == f"{prefix}-daemon-chat-concierge"


def test_legacy_run_names_remain_discoverable(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "home")
    assert run_name_candidates(home, "r7k2m9qp3", "github", vcs_kind="gitlab") == (
        run_name(home, "r7k2m9qp3", "github", vcs_kind="gitlab"),
        "sbxloop-r7k2m9qp3-gitlab",
        "sbxloop-r7k2m9qp3-github",
    )
