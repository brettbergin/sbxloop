"""Host preparation, preflighted before the unattended services (#898).

An account that can create the sbxloop home may still be unable to boot a
sandbox or keep a daemon alive after logout, and the two failures look
nothing alike: one is a kernel device an administrator has to grant, the
other a per-user service manager this session never reached. Before this,
`sbxloop init` invoked `systemctl --user` without establishing that the
manager was there, and recorded the systemd step as done with a failed
`loginctl enable-linger` appended to its notes — an unattended deployment
told its daemon persists when nothing had established that it does.

Every host here is synthetic: the device, the tool lookup and the two
systemd commands all come through :mod:`tests.fakes.fake_host`, so nothing
is asserted about the runner and nothing on the runner is changed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sbxloop.homeinit import HomeInit, InitError, InitOptions
from sbxloop.hostprep import DENIED, MISSING, READY, UNKNOWN, Capability, HostPrep
from sbxloop.paths import SbxloopHome
from tests.fakes.fake_host import MANAGER_DEGRADED, MANAGER_OFFLINE, fake_prep


class TestVirtualisationDevice:
    """A sandbox is a microVM: no openable /dev/kvm, no run."""

    def test_a_ready_device_is_ready(self) -> None:
        assert fake_prep(kvm="open").kvm().status == READY

    def test_an_absent_device_names_the_administrator_task(self) -> None:
        capability = fake_prep(kvm="absent").kvm()

        assert capability.status == MISSING and not capability.ready
        assert "/dev/kvm does not exist" in capability.detail
        assert capability.admin and "nested virtualisation" in capability.remedy

    def test_a_device_this_account_cannot_open_is_a_different_diagnosis(self) -> None:
        """The common one: the node is there and handed to a group this
        account is not in yet. It is *not* "missing", and the remedy is not
        "enable virtualisation"."""
        capability = fake_prep(kvm="denied").kvm()

        assert capability.status == DENIED and not capability.ready
        assert "cannot open it for reading and writing" in capability.detail
        assert "kvm` group" in capability.remedy and "next login" in capability.remedy

    def test_access_is_asked_of_the_kernel_not_of_a_group_list(self) -> None:
        """A group added in this shell is not in this process's credentials,
        so membership proves nothing about right now. The probe opens the
        question with the access mode a microVM needs."""
        asked: list[tuple[Path, int]] = []

        def access(path: Path, mode: int) -> bool:
            asked.append((path, mode))
            return True

        HostPrep(
            system="Linux",
            env={},
            access=access,
            exists=lambda _path: True,
            kvm_device=Path("/dev/kvm"),
        ).kvm()

        assert asked == [(Path("/dev/kvm"), os.R_OK | os.W_OK)]

    def test_a_device_that_cannot_be_inspected_is_unknown_never_ready(self) -> None:
        capability = fake_prep(kvm="unreadable").kvm()

        assert capability.status == UNKNOWN and not capability.ready
        assert "could not be inspected" in capability.detail


class TestFilesystemTools:
    def test_a_missing_mkfs_names_the_package(self) -> None:
        capability = fake_prep(mkfs=None).filesystem_tools()

        assert capability.status == MISSING and capability.admin
        assert "e2fsprogs" in capability.remedy
        assert "sbx installer refuses" in capability.remedy

    def test_a_tool_only_under_sbin_is_ready_and_says_so(self) -> None:
        """Debian keeps /usr/sbin off a non-root PATH and sbxloop adds it
        back for Docker's installer — a host like that is ready, and a
        preflight that faulted it would be wrong."""
        capability = fake_prep(mkfs="/usr/sbin/mkfs.ext4").filesystem_tools()

        assert capability.ready
        assert "off this account's PATH" in capability.detail

    def test_a_tool_on_the_accounts_own_path_just_passes(self) -> None:
        capability = fake_prep(mkfs="/usr/bin/mkfs.ext4", mkfs_on_path=True).filesystem_tools()

        assert capability.ready and capability.detail == "/usr/bin/mkfs.ext4"

    def test_the_lookup_adds_the_sbin_directories(self) -> None:
        seen: list[str | None] = []

        def which(name: str, path: str | None = None) -> str | None:
            seen.append(path)
            return "/usr/sbin/mkfs.ext4"

        HostPrep(system="Linux", env={"PATH": "/usr/bin"}, which=which).filesystem_tools()

        assert seen[0] is not None and seen[0].startswith("/usr/sbin:/sbin:/usr/bin")


class TestUserServiceManager:
    def test_a_reachable_manager_is_ready(self) -> None:
        assert fake_prep().user_manager().ready

    def test_a_degraded_manager_is_still_reachable(self) -> None:
        """`is-system-running` exits non-zero for a degraded manager, which
        is a host with a failed unit — not a host without a manager. Reading
        the exit code alone would refuse to install on it."""
        capability = fake_prep(manager=MANAGER_DEGRADED).user_manager()

        assert capability.ready and MANAGER_DEGRADED in capability.detail

    def test_a_session_with_no_manager_is_not_ready(self) -> None:
        capability = fake_prep(manager=MANAGER_OFFLINE).user_manager()

        assert capability.status == DENIED and not capability.ready
        assert "did not reach this account's manager" in capability.detail
        assert "XDG_RUNTIME_DIR" in capability.remedy and "--no-systemd" in capability.remedy

    def test_a_host_without_systemd_says_so_and_points_at_no_systemd(self) -> None:
        capability = fake_prep(systemctl=None).user_manager()

        assert capability.status == MISSING and not capability.admin
        assert "does not run systemd" in capability.detail
        assert "--no-systemd" in capability.remedy

    def test_a_probe_that_cannot_run_is_unknown(self) -> None:
        def run(_argv: Any) -> subprocess.CompletedProcess[str]:
            raise OSError("no such file")

        capability = HostPrep(
            system="Linux", env={}, run=run, which=lambda _n, path=None: "/bin/systemctl"
        ).user_manager()

        assert capability.status == UNKNOWN and not capability.ready


class TestLingering:
    def test_lingering_on_is_ready(self) -> None:
        capability = fake_prep(linger="yes").lingering("bergs")

        assert capability.ready and "lingering is on for bergs" in capability.detail

    def test_lingering_off_is_not_persistence(self) -> None:
        capability = fake_prep(linger="no").lingering("bergs")

        assert capability.status == MISSING and not capability.ready
        assert "stop at logout" in capability.detail
        assert "loginctl enable-linger bergs" in capability.remedy

    def test_an_unreadable_state_is_unknown_never_ready(self) -> None:
        capability = fake_prep(linger=None).lingering("bergs")

        assert capability.status == UNKNOWN and not capability.ready

    def test_an_unparsable_answer_is_unknown_too(self) -> None:
        capability = fake_prep(linger="").lingering("bergs")

        assert capability.status == UNKNOWN and not capability.ready

    def test_a_session_with_no_account_cannot_be_told_it_lingers(self) -> None:
        capability = fake_prep(env={"PATH": "/usr/bin"}).lingering("")

        assert capability.status == UNKNOWN and not capability.ready
        assert "names no account" in capability.detail


class TestPlatformScoping:
    """A check that does not apply to this platform must not judge it."""

    @pytest.mark.parametrize("system", ["Darwin", "Windows"])
    def test_nothing_linux_specific_applies_elsewhere(self, system: str) -> None:
        prep = fake_prep(system=system, kvm="absent", mkfs=None, systemctl=None, linger="no")
        capabilities = [*prep.sandbox_capabilities(), *prep.service_capabilities("bergs")]

        assert not any(c.applicable for c in capabilities)
        assert all(system in c.detail for c in capabilities)

    def test_a_capability_that_does_not_apply_is_never_read_as_ready(self) -> None:
        capability = fake_prep(system="Darwin").kvm()

        assert not capability.applicable and not capability.ready


class TestMessage:
    def test_the_row_carries_what_is_wrong_then_who_fixes_it(self) -> None:
        capability = Capability("kvm", MISSING, "it is not there", "install it", admin=True)

        assert capability.message == "it is not there — needs an administrator: install it"

    def test_an_operator_remedy_does_not_claim_to_need_an_administrator(self) -> None:
        capability = Capability("x", MISSING, "gone", "put it back")

        assert capability.message == "gone — put it back"


# -- what init does with it ---------------------------------------------------------


def init_on(tmp_path: Path, host: HostPrep, **overrides: Any) -> tuple[HomeInit, list[list[str]]]:
    """`sbxloop init` against a synthetic host, with every command it would
    run recorded instead of run.

    The interpreter step is already satisfied — init runs from the home's
    own venv — so these tests are about the host, not about uv.
    """
    calls: list[list[str]] = []

    def run(argv: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess([str(a) for a in argv], 0, "", "")

    home = SbxloopHome(tmp_path / "home")
    home.venv.mkdir(parents=True)
    init = HomeInit(
        home,
        InitOptions(**{"sbx": False, **overrides}),
        env={"HOME": str(tmp_path), "USER": "bergs", "PATH": "/usr/bin"},
        run=run,
        fetch=lambda _url, _target: None,
        system="Linux",
        machine="x86_64",
        sys_prefix=home.venv,
        user_units=tmp_path / "units",
        prep=host,
    )
    return init, calls


class TestInitPreflight:
    def test_an_unprepared_device_is_reported_without_stopping_the_layout(
        self, tmp_path: Path
    ) -> None:
        """The home is still worth having on a host whose administrator has
        not enabled virtualisation yet: doctor runs from it, config is
        edited in it. The note is what makes the gap visible."""
        init, _calls = init_on(tmp_path, fake_prep(kvm="denied"))

        report = init.execute()

        assert (tmp_path / "home" / "config").exists()
        assert any("host preparation needed — kvm" in note for note in report.notes)
        assert any("kvm` group" in note for note in report.notes)

    def test_a_host_without_mkfs_refuses_the_sbx_install_by_name(self, tmp_path: Path) -> None:
        """Docker's installer refuses outright without it; refusing here
        names the package instead of relaying that after a download."""
        init, _calls = init_on(tmp_path, fake_prep(mkfs=None), sbx=True)

        with pytest.raises(InitError, match="e2fsprogs"):
            init.execute()

    def test_the_filesystem_tool_is_irrelevant_to_an_install_without_sbx(
        self, tmp_path: Path
    ) -> None:
        init, _calls = init_on(tmp_path, fake_prep(mkfs=None), sbx=False)

        assert init.execute().done  # no refusal: nothing here formats anything

    def test_nothing_in_the_preflight_changes_the_host(self, tmp_path: Path) -> None:
        init, calls = init_on(tmp_path, fake_prep(kvm="absent", mkfs=None), sbx=False)

        init.execute()

        executables = [call[0] for call in calls]
        assert not any(
            name in executables for name in ("sudo", "usermod", "gpasswd", "modprobe", "systemctl")
        )
        assert not any(call[0].endswith("install.sh") for call in calls)
        assert not (tmp_path / "home" / "sbx").exists()


class TestInitUserServices:
    def test_systemd_on_a_session_with_no_manager_fails_by_name(self, tmp_path: Path) -> None:
        """Before: `systemctl --user daemon-reload` was invoked blind and
        the operator got its bus error. Now the request is refused with what
        is wrong and the way to install anyway."""
        init, calls = init_on(tmp_path, fake_prep(manager=MANAGER_OFFLINE), systemd=True)

        with pytest.raises(InitError, match="per-user service manager is not usable"):
            init.execute()

        assert not any(call[:2] == ["systemctl", "--user"] for call in calls)

    def test_a_host_without_systemd_at_all_is_refused_the_same_way(self, tmp_path: Path) -> None:
        init, _calls = init_on(tmp_path, fake_prep(systemctl=None), systemd=True)

        with pytest.raises(InitError, match="--no-systemd"):
            init.execute()

    def test_no_systemd_never_asks_the_service_manager(self, tmp_path: Path) -> None:
        """Scoping: a host with no user manager installs perfectly well
        without user units, so nothing about one may block it."""
        init, calls = init_on(
            tmp_path, fake_prep(systemctl=None, loginctl=None, linger=None), systemd=False
        )

        report = init.execute()

        assert "record" in report.done
        assert not any("linger" in " ".join(call) for call in calls)
        assert not any("persistence" in note for note in report.notes)

    def test_lingering_is_recorded_only_once_it_reads_back_on(self, tmp_path: Path) -> None:
        init, calls = init_on(tmp_path, fake_prep(linger="yes"), systemd=True)

        report = init.execute()

        assert ["loginctl", "enable-linger", "bergs"] in calls
        assert "lingering (bergs)" in report.done

    def test_lingering_that_stays_off_is_not_recorded_as_persistence(self, tmp_path: Path) -> None:
        """The failure this issue is about: the command is taken, the step
        reads done, and the daemon dies at the operator's next logout."""
        init, _calls = init_on(tmp_path, fake_prep(linger="no"), systemd=True)

        report = init.execute()

        assert not any("lingering" in done for done in report.done)
        assert any("unattended persistence is not confirmed" in n for n in report.notes)
        assert any("stop at logout" in n for n in report.notes)

    def test_a_refused_enable_linger_is_reported_and_not_papered_over(self, tmp_path: Path) -> None:
        host = fake_prep(linger="no")
        init, _calls = init_on(tmp_path, host, systemd=True)
        real_run = init.run

        def run(argv: Any) -> subprocess.CompletedProcess[str]:
            if list(argv)[:1] == ["loginctl"]:
                raise subprocess.CalledProcessError(1, list(argv), stderr="polkit said no")
            return real_run(argv)

        init.run = run  # type: ignore[method-assign]
        report = init.execute()

        assert any("enable-linger bergs failed" in n for n in report.notes)
        assert any("unattended persistence is not confirmed" in n for n in report.notes)

    def test_lingering_that_cannot_be_read_is_not_persistence_either(self, tmp_path: Path) -> None:
        """Fail closed on "could not tell": an unreadable state is reported
        as unconfirmed, never as set up."""
        init, _calls = init_on(tmp_path, fake_prep(linger=None), systemd=True)

        report = init.execute()

        assert not any("lingering" in done for done in report.done)
        assert any("unattended persistence is not confirmed" in n for n in report.notes)

    def test_a_session_that_names_no_account_confirms_nothing(self, tmp_path: Path) -> None:
        init, calls = init_on(tmp_path, fake_prep(env={"PATH": "/usr/bin"}), systemd=True)
        init.env.pop("USER")

        report = init.execute()

        assert not any("linger" in " ".join(call) for call in calls)
        assert any("names no account" in n for n in report.notes)


# -- what doctor shows ---------------------------------------------------------------


@pytest.fixture
def on_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point doctor's own ``HostPrep`` lookups at a synthetic host."""

    def use(**host: Any) -> None:
        monkeypatch.setattr(
            "sbxloop.hostprep.HostPrep", lambda **_kwargs: fake_prep(**host), raising=True
        )

    return use


class TestDoctorRows:
    def test_an_unprepared_device_is_a_row_that_says_who_fixes_it(self, on_host: Any) -> None:
        from sbxloop.cli.doctor import host_prep_checks

        on_host(kvm="denied")
        rows = {check.name: check for check in host_prep_checks({})}

        assert not rows["host kvm"].ok
        assert "needs an administrator" in rows["host kvm"].detail
        assert rows["host kvm"].hard is False  # a diagnosis, not a gate

    def test_a_prepared_linux_host_passes_both_rows(self, on_host: Any) -> None:
        from sbxloop.cli.doctor import host_prep_checks

        on_host()
        assert all(check.ok for check in host_prep_checks({}))

    def test_no_linux_rows_off_linux(self, on_host: Any) -> None:
        from sbxloop.cli.doctor import host_prep_checks

        on_host(system="Darwin", kvm="absent", mkfs=None)
        assert host_prep_checks({}) == []

    def test_the_service_rows_appear_only_where_units_were_asked_for(
        self, tmp_path: Path, on_host: Any
    ) -> None:
        """A `--no-systemd` home has no units, so judging it against a user
        manager and lingering would fail a supported mode."""
        from sbxloop.cli.doctor import home_checks
        from sbxloop.homeinit import UNIT_NAMES

        on_host(manager=MANAGER_OFFLINE, linger="no")
        home = SbxloopHome(tmp_path / "home")
        home.ensure_tree()
        home.write_record(sbxloop_version="1.2.3", created_by="test")
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin", "USER": "bergs"}

        named = {check.name for check in home_checks(home, env)}
        assert "user service manager" not in named and "unattended persistence" not in named

        for name in UNIT_NAMES:
            home.unit(name).write_text("[Unit]\n")
        rows = {check.name: check for check in home_checks(home, env)}
        assert not rows["user service manager"].ok
        assert "--no-systemd" in rows["user service manager"].detail
        assert not rows["unattended persistence"].ok
        assert "stop at logout" in rows["unattended persistence"].detail

    def test_lingering_that_is_on_reads_as_persistence(self, tmp_path: Path, on_host: Any) -> None:
        from sbxloop.cli.doctor import home_checks
        from sbxloop.homeinit import UNIT_NAMES

        on_host(linger="yes")
        home = SbxloopHome(tmp_path / "home")
        home.ensure_tree()
        home.write_record(sbxloop_version="1.2.3", created_by="test")
        for name in UNIT_NAMES:
            home.unit(name).write_text("[Unit]\n")

        rows = {c.name: c for c in home_checks(home, {"HOME": str(tmp_path), "USER": "bergs"})}

        assert rows["unattended persistence"].ok
