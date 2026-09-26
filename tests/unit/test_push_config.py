"""`[push]`: off by default, and a relay named only by an http(s) URL."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.config import Config


def test_push_is_off_and_names_no_relay_by_default() -> None:
    push = Config().push
    assert push.enabled is False
    assert push.relay_url == ""
    assert push.available is False


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("http://relay.internal:8080", "http://relay.internal:8080"),
        ("https://relay.example.test/", "https://relay.example.test"),
        ("  https://relay.example.test/base/  ", "https://relay.example.test/base"),
        ("", ""),
    ],
)
def test_a_relay_url_is_an_http_origin(given: str, stored: str) -> None:
    assert Config.model_validate({"push": {"relay_url": given}}).push.relay_url == stored


@pytest.mark.parametrize(
    "bad",
    [
        "relay.internal:8080",
        "ftp://relay.internal",
        "https://",
        "https://relay.example.test/?key=value",
        "https://relay.example.test/#frag",
        "https://user:pass@relay.example.test",
    ],
)
def test_anything_else_is_refused_at_load(bad: str) -> None:
    with pytest.raises(ValidationError, match=r"push\.relay_url"):
        Config.model_validate({"push": {"relay_url": bad}})


def test_push_is_available_only_when_on_with_a_relay() -> None:
    on = Config.model_validate({"push": {"enabled": True, "relay_url": "http://r.test"}})
    assert on.push.available is True
    assert Config.model_validate({"push": {"enabled": True}}).push.available is False
    assert Config.model_validate({"push": {"relay_url": "http://r.test"}}).push.available is False


@pytest.mark.parametrize(
    "bad",
    [
        {"timeout_s": 0},
        {"max_attempts": 0},
        {"backoff_s": 0},
        {"backoff_max_s": 0},
        {"max_devices_per_user": 0},
    ],
)
def test_the_bounds_are_positive(bad: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"push": bad})
