"""Release drift: installed versus latest on PyPI.

The network is never touched here — ``fetch`` is injected into VersionProbe,
and the one test that exercises the real HTTP path monkeypatches
``urllib.request.urlopen`` the way the worker's REST tests do.
"""

from __future__ import annotations

import io
import urllib.error
from typing import Any

import pytest

import sbxloop
from sbxloop.daemon import versions
from sbxloop.daemon.versions import (
    MAX_BYTES,
    PYPI_TTL_S,
    UNBUILT,
    VersionProbe,
    behind_by,
    compare,
    fetch_latest,
    start_drift_check,
)
from sbxloop.errors import SbxError, SbxNotFoundError


class TestCompare:
    @pytest.mark.parametrize(
        ("installed", "latest", "verdict"),
        [
            ("0.7.12", "0.7.15", "behind"),
            ("0.7.15", "0.7.15", "current"),
            ("0.8.0", "0.7.15", "ahead"),
            ("1.0.0", "0.9.9", "ahead"),
            ("0.9.9", "1.0.0", "behind"),
            # the row a string comparison gets wrong
            ("0.10.0", "0.9.9", "ahead"),
            ("0.9.9", "0.10.0", "behind"),
            # a pre/post release is not what `pip install --upgrade` fetches,
            # so ranking against it would be advice that does not work
            ("0.7.14", "0.8.0rc1", "unknown"),
            ("0.7.14", "0.7.14.post1", "unknown"),
            # A dev build is named for the version it is heading TOWARD, so it
            # is not the release it truncates to and never earns an upgrade
            # verdict in either direction.
            ("0.7.12.dev0", "0.7.15", "dev"),
            ("0.7.12.dev0", "0.7.12", "dev"),
            ("0.7.12.dev0", "0.7.11", "dev"),
            # The never-built fallback is not a version anyone shipped.
            (UNBUILT, "0.7.15", "unknown"),
            ("", "0.7.15", "unknown"),
            ("not-a-version", "0.7.15", "unknown"),
            ("0.7.12", "not-a-version", "unknown"),
            # No answer from PyPI means no verdict, never a false "behind".
            ("0.7.12", None, "unknown"),
        ],
    )
    def test_verdicts(self, installed: str, latest: str | None, verdict: str) -> None:
        assert compare(installed, latest) == verdict

    @pytest.mark.parametrize(
        ("installed", "latest", "gap"),
        [
            ("0.7.12", "0.7.15", 3),
            ("0.7.14", "0.7.15", 1),
            ("0.7.15", "0.7.15", None),  # not behind
            ("0.7.15", "0.7.12", None),  # ahead
            ("0.7.12", "0.8.1", None),  # a minor bump is not "N patches"
            ("0.7.12", None, None),
            ("garbage", "0.7.15", None),
        ],
    )
    def test_behind_by_counts_only_patch_runs(
        self, installed: str, latest: str | None, gap: int | None
    ) -> None:
        assert behind_by(installed, latest) == gap


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class TestFetchLatest:
    def test_reads_info_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            captured["url"] = request.full_url
            captured["agent"] = request.get_header("User-agent")
            captured["timeout"] = timeout
            return FakeResponse(b'{"info": {"version": "0.7.15"}, "releases": {}}')

        monkeypatch.setattr(versions.urllib.request, "urlopen", fake_urlopen)
        assert fetch_latest("sbxloop") == "0.7.15"
        assert captured["url"] == "https://pypi.org/pypi/sbxloop/json"
        assert captured["url"].startswith("https://")  # never a credential over plaintext
        assert sbxloop.__version__ in captured["agent"]
        assert captured["timeout"] == versions.PYPI_TIMEOUT_S

    @pytest.mark.parametrize(
        "boom",
        [
            urllib.error.HTTPError("https://pypi.org", 503, "down", None, io.BytesIO(b"")),
            urllib.error.URLError("no route to host"),
            TimeoutError("timed out"),
        ],
        ids=["http_error", "url_error", "timeout"],
    )
    def test_network_failure_is_none_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, boom: Exception
    ) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise boom

        monkeypatch.setattr(versions.urllib.request, "urlopen", fake_urlopen)
        assert fetch_latest("sbxloop") is None

    def test_unparseable_and_oversized_bodies_are_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def garbage(request: Any, timeout: float = 0) -> FakeResponse:
            return FakeResponse(b"<html>not json</html>")

        monkeypatch.setattr(versions.urllib.request, "urlopen", garbage)
        assert fetch_latest("sbxloop") is None

        def huge(request: Any, timeout: float = 0) -> FakeResponse:
            return FakeResponse(b"x" * (MAX_BYTES + 1))

        monkeypatch.setattr(versions.urllib.request, "urlopen", huge)
        assert fetch_latest("sbxloop") is None

    def test_missing_info_key_is_a_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_info(request: Any, timeout: float = 0) -> FakeResponse:
            return FakeResponse(b'{"releases": {}}')

        monkeypatch.setattr(versions.urllib.request, "urlopen", no_info)
        assert fetch_latest("sbxloop") is None


class FakeSbx:
    def __init__(self, answer: str | None = "0.38.1", raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.timeouts: list[float | None] = []

    def version(self, *, timeout: float | None = None) -> str | None:
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        return self.answer


def probe(
    latest: dict[str, str | None],
    *,
    sbx: Any = None,
    now: list[float] | None = None,
    check_pypi: bool = True,
    upgrade_command: str | None = None,
) -> tuple[VersionProbe, list[str]]:
    """A probe whose PyPI answers are canned; returns it plus the call log."""
    calls: list[str] = []

    def fetch(name: str) -> str | None:
        calls.append(name)
        return latest.get(name)

    clock = (lambda: now[0]) if now is not None else (lambda: 1000.0)
    return VersionProbe(
        sbx=sbx,
        clock=clock,
        fetch=fetch,
        check_pypi=check_pypi,
        upgrade_command=upgrade_command,
    ), calls


class TestProbe:
    def test_installed_reads_both_distributions_and_sbx(self) -> None:
        sbx = FakeSbx("0.38.1")
        p, _ = probe({}, sbx=sbx)
        installed = p.installed()
        assert installed.sbxloop == sbxloop.__version__
        assert installed.worker == sbxloop.__version__  # lockstep, see test_version.py
        assert installed.sbx == "0.38.1"
        # a latency budget, not the CLI's 120s default
        assert sbx.timeouts == [5.0]

    @pytest.mark.parametrize(
        ("boom", "reads"),
        [
            (SbxNotFoundError("no sbx on PATH"), "not found on PATH"),
            (SbxError("sbx version timed out after 5s"), "could not be asked"),
        ],
        ids=["missing", "wedged"],
    )
    def test_a_broken_sbx_says_which_and_does_not_sink_the_report(
        self, boom: Exception, reads: str
    ) -> None:
        """A wedged Docker daemon is not a missing binary; the prose says so,
        and either way the PyPI rows — the actual point — still render."""
        p, _ = probe({"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"}, sbx=FakeSbx(raises=boom))
        assert p.installed().sbx is None
        text = p.summary()
        assert reads in text
        assert "0.7.15 on PyPI" in text

    def test_no_sbx_handle_reports_nothing_for_it(self) -> None:
        p, _ = probe({})
        assert p.installed().sbx is None
        assert "not configured for this daemon" in p.summary()

    def test_latest_memoises_successes_until_the_ttl_expires(self) -> None:
        now = [1000.0]
        p, calls = probe({"sbxloop": "0.7.15"}, now=now)
        assert p.latest("sbxloop") == "0.7.15"
        assert p.latest("sbxloop") == "0.7.15"
        assert calls == ["sbxloop"]  # one network call, not two
        now[0] += PYPI_TTL_S - 1
        p.latest("sbxloop")
        assert calls == ["sbxloop"]
        now[0] += 2
        p.latest("sbxloop")
        assert calls == ["sbxloop", "sbxloop"]

    def test_a_failed_lookup_is_not_cached(self) -> None:
        """Caching a blip would leave the tool useless for five minutes."""
        p, calls = probe({"sbxloop": None})
        assert p.latest("sbxloop") is None
        assert p.latest("sbxloop") is None
        assert calls == ["sbxloop", "sbxloop"]


class TestSummary:
    def test_behind_names_the_gap_and_who_must_act(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", "0.7.12")
        p, _ = probe({"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"}, sbx=FakeSbx("0.38.1"))
        text = p.summary()
        assert (
            "sbxloop         0.7.12 installed · 0.7.15 on PyPI · BEHIND by 3 patch releases" in text
        )
        assert "sbxloop-worker  0.7.12 installed · 0.7.15 on PyPI · BEHIND" in text
        assert "sbx CLI         0.38.1" in text
        # #638: no install method is guessed — pip is one of several.
        assert "pip install --upgrade" not in text
        assert "depends on how sbxloop was installed" in text
        assert "operator's step on the daemon host" in text
        assert "You cannot do it from here" in text

    def test_behind_names_the_configured_upgrade_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #638/#641: `[daemon] upgrade_command` is what the advice says to run.
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", "0.7.12")
        p, _ = probe(
            {"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"},
            upgrade_command="pipx upgrade sbxloop",
        )
        text = p.summary()
        assert "run `pipx upgrade sbxloop`, then restart the daemon" in text
        assert "depends on how sbxloop was installed" not in text

    def test_check_off_asks_nothing_and_advises_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #641: `[daemon] version_check = false` — zero outbound HTTP, the
        # installed half still answers, and no upgrade is inferred.
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", "0.7.12")
        p, calls = probe({"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"}, check_pypi=False)
        text = p.summary()
        assert calls == []
        assert "sbxloop         0.7.12 installed · PyPI not checked" in text
        assert "[daemon] version_check = false" in text
        assert "could not reach PyPI" not in text
        assert "BEHIND" not in text and "depends on how" not in text
        assert p.drift_notice() is None and calls == []

    def test_current_says_so_without_an_upgrade_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.15")
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", "0.7.15")
        p, _ = probe({"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"})
        text = p.summary()
        assert "up to date" in text
        assert "operator's step" not in text

    def test_a_dev_build_never_advises_an_upgrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12.dev0")
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", "0.7.12.dev0")
        p, _ = probe({"sbxloop": "0.7.12", "sbxloop-worker": "0.7.12"})
        text = p.summary()
        assert "a development build, not a release" in text
        assert "operator's step" not in text
        assert "up to date" not in text  # the trap: 0.7.12.dev0 is NOT 0.7.12

    def test_unreachable_pypi_keeps_the_installed_half(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        p, _ = probe({})
        text = p.summary()
        assert "0.7.12 installed · could not reach PyPI" in text
        assert "the installed versions are still accurate" in text
        assert "operator's step" not in text

    def test_an_unbuilt_tree_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", UNBUILT)
        monkeypatch.setattr(versions.sbxloop_worker, "__version__", UNBUILT)
        p, _ = probe({"sbxloop": "0.7.15", "sbxloop-worker": "0.7.15"})
        text = p.summary()
        assert "never built" in text
        assert "operator's step" not in text


class TestDriftNotice:
    def test_only_a_behind_host_gets_a_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        p, _ = probe({"sbxloop": "0.7.15"})
        notice = p.drift_notice()
        assert notice is not None
        assert "0.7.12" in notice and "0.7.15" in notice
        assert "3 patch releases behind" in notice
        assert "pip install --upgrade" not in notice
        assert "depends on how sbxloop was installed" in notice

    def test_the_notice_names_the_configured_upgrade_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        p, _ = probe({"sbxloop": "0.7.15"}, upgrade_command="~/bin/upgrade-sbxloop")
        notice = p.drift_notice()
        assert notice is not None
        assert "run `~/bin/upgrade-sbxloop`" in notice

    @pytest.mark.parametrize(
        ("installed", "latest"),
        [("0.7.15", "0.7.15"), ("0.8.0", "0.7.15"), ("0.7.12.dev0", "0.7.15"), ("0.7.12", None)],
        ids=["current", "ahead", "dev_build", "pypi_down"],
    )
    def test_quiet_otherwise(
        self, monkeypatch: pytest.MonkeyPatch, installed: str, latest: str | None
    ) -> None:
        monkeypatch.setattr(sbxloop, "__version__", installed)
        p, _ = probe({"sbxloop": latest})
        assert p.drift_notice() is None


class TestStartDriftCheck:
    def test_notifies_once_when_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        p, _ = probe({"sbxloop": "0.7.15"})
        seen: list[str] = []
        start_drift_check(p, seen.append).join(timeout=5)
        assert len(seen) == 1 and "0.7.15" in seen[0]

    def test_says_nothing_when_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.15")
        p, _ = probe({"sbxloop": "0.7.15"})
        seen: list[str] = []
        start_drift_check(p, seen.append).join(timeout=5)
        assert seen == []

    def test_a_thrown_probe_never_reaches_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The daemon must start even if the check itself is broken."""

        def boom(name: str) -> str | None:
            raise RuntimeError("probe exploded")

        p = VersionProbe(clock=lambda: 1000.0, fetch=boom)
        seen: list[str] = []
        thread = start_drift_check(p, seen.append)
        thread.join(timeout=5)
        assert not thread.is_alive() and seen == []

    def test_runs_without_a_frontend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sbxloop, "__version__", "0.7.12")
        p, _ = probe({"sbxloop": "0.7.15"})
        start_drift_check(p, None).join(timeout=5)  # logs only; must not raise

    def test_the_thread_is_a_daemon_so_shutdown_never_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p, _ = probe({"sbxloop": "0.7.15"})
        assert start_drift_check(p, None).daemon is True
