"""Private files under the platform's own access control
(:mod:`sbxloop.hostfiles`).

The Windows branch is driven through the ``runner`` seam on every host, so
the POSIX shards cover it; the rows marked ``windows_host`` assert the facts
only a real Windows runner can settle — that ``chmod`` there does not
produce ``0600``, and that the branch this code takes is the ACL one.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from sbxloop.hostfiles import (
    PRIVATE_FILE_MODE,
    PrivacyError,
    create_private,
    make_private,
    privacy,
)


class FakeIcacls:
    """Stands in for ``icacls``: records argv, answers with a canned listing."""

    def __init__(self, *, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.code, self.stdout, self.stderr)


def _listing(path: Path, *aces: str) -> str:
    """An ``icacls <path>`` listing: the path on the first ACE's line, the
    rest indented, and the summary the real tool prints."""
    lines = [f"{path} {aces[0]}"] + [f"{' ' * len(str(path))} {ace}" for ace in aces[1:]]
    return "\n".join([*lines, "", "Successfully processed 1 files; Failed processing 0 files"])


def _me() -> str:
    import getpass

    return getpass.getuser()


class TestPosix:
    def test_a_0600_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.touch(mode=PRIVATE_FILE_MODE)
        verdict = privacy(path, os_name="posix")
        assert verdict.ok and verdict.detail == "mode 0600" and verdict.remedy == ""

    def test_a_group_readable_file_is_not_and_the_remedy_is_chmod(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.touch()
        path.chmod(0o640)
        verdict = privacy(path, os_name="posix")
        assert verdict.private is False
        assert verdict.detail == "mode 0640"
        assert verdict.remedy == f"chmod 600 {path}"

    def test_a_file_that_cannot_be_read_is_could_not_tell(self, tmp_path: Path) -> None:
        verdict = privacy(tmp_path / "gone.env", os_name="posix")
        assert verdict.private is None and not verdict.ok

    def test_make_private_sets_the_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.write_text("TOKEN=x\n")
        path.chmod(0o644)
        make_private(path, os_name="posix")
        assert path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE

    def test_create_private_never_leaves_a_readable_window(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        create_private(path, os_name="posix")
        assert path.is_file() and path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE


class TestWindows:
    """The ACL branch, driven on any host through the runner seam."""

    def test_an_acl_naming_only_this_user_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls(stdout=_listing(path, f"HOST\\{_me()}:(F)"))
        verdict = privacy(path, os_name="nt", runner=icacls)
        assert verdict.ok and verdict.remedy == ""
        assert icacls.calls == [["icacls", str(path)]]

    def test_the_machine_identities_do_not_make_it_public(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls(
            stdout=_listing(
                path,
                "NT AUTHORITY\\SYSTEM:(F)",
                "BUILTIN\\Administrators:(F)",
                f"{_me()}:(F)",
            )
        )
        assert privacy(path, os_name="nt", runner=icacls).ok

    def test_owner_rights_is_the_owner_not_a_stranger(self, tmp_path: Path) -> None:
        """Windows leaves an `OWNER RIGHTS` (S-1-3-4) entry on a file
        restricted with `/inheritance:r /grant:r` — the shape make_private
        produces. It names whoever owns the object, not a third party, so
        reading it as one reported every correctly-restricted file as
        readable by someone else (found by the windows-host CI job)."""
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls(stdout=_listing(path, f"HOST\\{_me()}:(F)", "OWNER RIGHTS:(F)"))
        verdict = privacy(path, os_name="nt", runner=icacls)
        assert verdict.ok, verdict.detail

    def test_another_principal_makes_it_not_private(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls(
            stdout=_listing(path, f"HOST\\{_me()}:(F)", "BUILTIN\\Users:(RX)", "Everyone:(R)")
        )
        verdict = privacy(path, os_name="nt", runner=icacls)
        assert verdict.private is False
        assert "BUILTIN\\Users" in verdict.detail and "Everyone" in verdict.detail
        assert verdict.remedy.startswith("icacls ")

    def test_an_unreadable_acl_is_could_not_tell_never_a_pass(self, tmp_path: Path) -> None:
        """Fail closed: a host that could not answer must not be reported as
        a host whose secrets file is safe."""
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls(code=1, stderr="secrets.env: Access is denied.")
        verdict = privacy(path, os_name="nt", runner=icacls)
        assert verdict.private is None and not verdict.ok
        assert "Access is denied" in verdict.detail

    def test_no_icacls_on_the_host_is_could_not_tell(self, tmp_path: Path) -> None:
        def missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("icacls")

        verdict = privacy(tmp_path / "secrets.env", os_name="nt", runner=missing)
        assert verdict.private is None

    def test_an_empty_listing_is_could_not_tell(self, tmp_path: Path) -> None:
        icacls = FakeIcacls(stdout="Successfully processed 1 files; Failed processing 0 files\n")
        assert privacy(tmp_path / "secrets.env", os_name="nt", runner=icacls).private is None

    def test_make_private_resets_inheritance_and_grants_only_this_user(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "secrets.env"
        path.touch()
        icacls = FakeIcacls()
        make_private(path, os_name="nt", runner=icacls)
        (argv,) = icacls.calls
        assert argv[:2] == ["icacls", str(path)]
        assert "/inheritance:r" in argv and "/grant:r" in argv
        assert any(word.endswith(":F") and _me() in word for word in argv)

    def test_make_private_raises_rather_than_leaving_a_credential_exposed(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "secrets.env"
        path.touch()
        icacls = FakeIcacls(code=5, stderr="Access is denied.")
        with pytest.raises(PrivacyError, match="Access is denied"):
            make_private(path, os_name="nt", runner=icacls)

    def test_create_private_creates_then_restricts(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        icacls = FakeIcacls()
        create_private(path, os_name="nt", runner=icacls)
        assert path.is_file() and len(icacls.calls) == 1


class TestTimezoneDatabase:
    """`zoneinfo` reads the *system* tz database and Windows ships none, so
    the package carries `tzdata` there. Asserted from any host: the
    windows-host job proves the effect, this keeps the declaration from
    being dropped where nobody would notice until that job ran."""

    def test_the_package_declares_tzdata_for_windows(self) -> None:
        import tomllib

        root = Path(__file__).resolve().parents[2]
        text = (root / "packages" / "sbxloop" / "pyproject.toml").read_bytes()
        deps = tomllib.loads(text.decode())["project"]["dependencies"]
        (tz,) = [d for d in deps if d.split(";")[0].strip() == "tzdata"]
        assert "sys_platform" in tz and "win32" in tz


@pytest.mark.windows_host
@pytest.mark.skipif(os.name != "nt", reason="the facts only a Windows host settles")
class TestOnWindowsItself:
    def test_chmod_does_not_produce_0600_which_is_why_the_mode_check_went(
        self, tmp_path: Path
    ) -> None:
        """The observation behind this change: a mode gate on Windows can
        never pass, so the `chmod 600` it suggested could never clear it."""
        path = tmp_path / "secrets.env"
        path.touch()
        path.chmod(PRIVATE_FILE_MODE)
        assert path.stat().st_mode & 0o777 != PRIVATE_FILE_MODE

    def test_the_real_host_takes_the_acl_branch_and_reaches_a_verdict(self, tmp_path: Path) -> None:
        path = tmp_path / "secrets.env"
        path.write_text("TOKEN=x\n")
        make_private(path)
        verdict = privacy(path)
        # icacls is on every Windows host, so this is a yes or a no —
        # "could not tell" here means the ACL path itself is broken.
        assert verdict.private is not None, verdict.detail
        assert verdict.ok, verdict.detail
