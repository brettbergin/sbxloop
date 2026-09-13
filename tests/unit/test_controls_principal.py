"""A principal is who is asking; the attribution string is derived from it,
never the other way round."""

from __future__ import annotations

import pytest

from sbxloop.daemon.controls import ALL_CAPABILITIES, CAPABILITIES, WORKSPACE_ID, Principal
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.controls.service import require


class TestTrusted:
    @pytest.mark.parametrize(
        ("by", "via"),
        [
            ("brett via sbxloop daemon ctl", "ctl"),
            ("discord user `brett`", "discord"),
            ("slack user `U1`", "slack"),
            ("mattermost user `m`", "mattermost"),
            ("brett via sbxloop tui", "local"),
            ("ops", "concierge"),
        ],
    )
    def test_keeps_the_legacy_attribution_byte_for_byte(self, by: str, via: str) -> None:
        principal = Principal.trusted(by, via)
        assert principal.attribution() == by
        assert principal.via == via
        assert principal.kind == "operator"
        assert principal.capabilities == ALL_CAPABILITIES
        assert principal.workspace_id == WORKSPACE_ID

    def test_a_missing_attribution_stays_missing(self) -> None:
        """The loop's own ``by or "operator"`` fallbacks must still fire:
        a trusted surface with no name is not renamed here."""
        principal = Principal.trusted(None, "ctl")
        assert principal.attribution() is None
        assert principal.id == "ctl"

    def test_every_capability_is_granted(self) -> None:
        principal = Principal.trusted("x", "ctl")
        assert all(principal.can(cap) for cap in CAPABILITIES)


class TestScoped:
    def test_a_client_holds_only_what_it_was_granted(self) -> None:
        client = Principal(
            kind="client",
            id="cli_1",
            display="reporter",
            via="api",
            capabilities=frozenset({"runs:read"}),
        )
        assert client.can("runs:read")
        assert not client.can("runs:control")
        require(client, "runs:read")
        with pytest.raises(ControlError) as excinfo:
            require(client, "runs:control")
        assert excinfo.value.code == "forbidden"
        assert "runs:control" in excinfo.value.message
        assert excinfo.value.detail == {"capability": "runs:control"}

    def test_audit_fields_carry_no_capabilities(self) -> None:
        """The audit line says who; what they may do is policy, not record."""
        client = Principal(kind="client", id="cli_1", display=None, via="api")
        assert client.audit() == {
            "kind": "client",
            "id": "cli_1",
            "display": None,
            "via": "api",
            "workspace_id": "local",
        }

    def test_system_principal_has_no_attribution(self) -> None:
        assert Principal.system("schedule").attribution() is None
