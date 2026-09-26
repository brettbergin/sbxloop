"""Which hosts sbxloop runs on (#596): the verdict, the refusal at the
sandbox-needing commands, and the doctor row."""

from __future__ import annotations

import pytest

from sbxloop.cli.doctor import host_check
from sbxloop.hostos import host_support


class TestVerdict:
    def test_linux_and_macos_are_supported(self) -> None:
        assert host_support("Linux", "6.8.0-generic").supported
        assert host_support("Darwin", "25.6.0").supported
        assert host_support("Darwin", "25.6.0").system == "Darwin"

    def test_wsl2_is_a_supported_linux_with_a_note(self) -> None:
        support = host_support("Linux", "5.15.167.4-microsoft-standard-WSL2")
        assert support.supported and support.system == "WSL"
        assert "WSL integration" in support.detail

    def test_native_windows_names_its_backend_requirements(self) -> None:
        support = host_support("Windows", "11")
        assert support.supported and support.system == "Windows"
        assert "Hypervisor Platform" in support.detail

    def test_an_unknown_system_is_untested_not_refused(self) -> None:
        support = host_support("FreeBSD", "14.1")
        assert support.supported and "untested" in support.detail

    def test_the_live_host_is_judged_from_platform(self) -> None:
        support = host_support()
        assert support.system and support.detail


class TestWindowsDoctor:
    def test_doctor_row_names_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr("platform.release", lambda: "11")
        check = host_check()
        assert check.name == "host" and check.ok and not check.hard
        assert "Hypervisor Platform" in check.detail
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.release", lambda: "6.8.0-microsoft-standard-WSL2")
        check = host_check()
        assert check.ok and "WSL2" in check.detail
