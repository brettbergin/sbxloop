"""Keep the real-sbx CI runner able to boot and publish its verdicts."""

from pathlib import Path, PurePosixPath

import yaml

from sbxloop.paths import SbxloopHome

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/sbx-conformance.yml"


def test_daemon_socket_fits_the_unix_path_limit() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    env = workflow["env"]
    state = PurePosixPath(env.get("XDG_STATE_HOME", "/home/runner/.local/state"))
    socket = (
        state
        / "sandboxes"
        / f"sandboxes-{env['SBXLOOP_APP_NAME']}"
        / "sandboxd/containerd/containerd.sock.ttrpc"
    )
    # sbx 0.42.1 rejects this path before any conformance probes can run.
    assert len(str(socket).encode()) <= 104, socket


def test_artifact_upload_includes_the_versioned_verdict_cache(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / ".sbxloop")
    verdict = home.conformance / "sbx-0.42.1.json"
    verdict.write_text("{}")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    upload = next(
        step
        for step in workflow["jobs"]["conformance"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    uploaded = {
        path for pattern in upload["with"]["path"].splitlines() for path in tmp_path.glob(pattern)
    }
    assert verdict in uploaded
