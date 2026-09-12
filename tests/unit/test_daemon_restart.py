"""An operator's ``restart`` (#969) on the real loop: the marker it leaves,
the exit it asks for, the refusal when nothing would start the daemon
again, and what the process that comes back says about it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import RESTART_MARKER_KEY, UNSUPERVISED_REFUSAL
from tests.unit.test_daemon_loop import Harness, RecordingFrontend


def _harness(tmp_path: Path, **daemon: object) -> Harness:
    config = Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}, "daemon": daemon}
    )
    h = Harness(tmp_path, config)
    h.loop.frontend = RecordingFrontend()  # type: ignore[assignment]
    return h


def _notices(h: Harness) -> list[str]:
    return list(h.loop.frontend.seen)  # type: ignore[union-attr]


class TestSupervisor:
    def test_systemd_is_told_by_invocation_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        h = _harness(tmp_path)
        assert h.loop.supervisor() is None
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        assert h.loop.supervisor() == "systemd"

    def test_an_operator_can_declare_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert _harness(tmp_path, supervised=True).loop.supervisor() == "declared"


class TestRequest:
    def test_unsupervised_is_refused_and_nothing_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        h = _harness(tmp_path)
        with pytest.raises(ValueError, match="not under a service manager"):
            h.loop.request_restart(by="brett", reason="operator restart")
        assert not h.loop.stopping and not h.loop.restart_pending
        assert h.dstore.get_value(RESTART_MARKER_KEY) is None
        assert UNSUPERVISED_REFUSAL.startswith("this daemon is not under a service manager")

    def test_supervised_leaves_a_marker_and_asks_for_the_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        h = _harness(tmp_path)
        h.loop.request_restart(by="brett", reason="operator restart", key="daemon.x", value=1)
        assert h.loop.stopping and h.loop.restart_pending
        assert h.loop.status()["restarting"] is True and h.loop.status()["stopping"] is True
        raw = h.dstore.get_value(RESTART_MARKER_KEY)
        assert raw is not None
        marker = json.loads(raw)
        assert marker == {
            "by": "brett",
            "reason": "operator restart",
            "requested_at": h.clock(),
            "mode": "graceful",
            "supervisor": "systemd",
            "key": "daemon.x",
            "value": 1,
        }
        assert _notices(h) == [
            "restart requested by brett: operator restart — after the current run"
        ]

    def test_now_cancels_the_run_in_flight_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        h = _harness(tmp_path)
        h.loop.request_restart(by="brett", reason="operator restart", now=True)
        marker = json.loads(h.dstore.get_value(RESTART_MARKER_KEY) or "{}")
        assert marker["mode"] == "now"
        assert _notices(h)[-1].endswith("cancelling the current run first")


class TestComingBack:
    def test_the_next_start_says_who_asked_and_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        before = _harness(tmp_path)
        before.loop.request_restart(by="brett", reason="operator restart")
        # The process that comes back: same store, a fresh loop, and nothing
        # to do but start and say so (the stop flag makes run_forever return).
        after = _harness(tmp_path)
        after.clock.t = before.clock.t + 12.0
        after.loop.request_stop()
        after.loop.run_forever()
        assert _notices(after)[:2] == [
            "daemon started",
            "restarted by brett: operator restart — up again 12s after the request",
        ]
        notice = after.loop.frontend.notices[1]  # type: ignore[union-attr]
        assert notice.kind == "daemon.restarted" and notice.level == "info"
        # consumed: the start after this one is not a restart
        assert after.dstore.get_value(RESTART_MARKER_KEY) is None
        again = _harness(tmp_path)
        again.loop.request_stop()
        again.loop.run_forever()
        assert "restarted by" not in " ".join(_notices(again))

    def test_a_stale_marker_is_reported_not_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        before = _harness(tmp_path)
        before.loop.request_restart(by="brett", reason="operator restart")
        after = _harness(tmp_path)
        after.clock.t = before.clock.t + after.config.daemon.claim_stale_after_s + 1
        after.loop.request_stop()
        after.loop.run_forever()
        (stale,) = [n for n in after.loop.frontend.notices if "restart" in n.kind]  # type: ignore[union-attr]
        assert stale.kind == "daemon.restart_marker_stale" and stale.level == "warning"
        assert "never completed; this start is not it" in stale.text
        assert after.dstore.get_value(RESTART_MARKER_KEY) is None

    def test_a_marker_that_is_not_json_is_dropped(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        h.dstore.set_value(RESTART_MARKER_KEY, "not json")
        h.loop.request_stop()
        h.loop.run_forever()
        assert not any("restart" in n.kind for n in h.loop.frontend.notices)  # type: ignore[union-attr]
        assert h.dstore.get_value(RESTART_MARKER_KEY) is None
