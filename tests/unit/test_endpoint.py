"""The operator-named endpoint parser: what `[agent.openai] base_url`
accepts, what it refuses, and what each path reads off it."""

from __future__ import annotations

import pytest

from sbxloop.endpoint import host_kind, parse_endpoint


@pytest.mark.parametrize(
    ("url", "host", "port", "kind", "normalised"),
    [
        (
            "https://models.example.com/v1",
            "models.example.com",
            None,
            "domain",
            "https://models.example.com/v1",
        ),
        ("http://vllm:8000/v1", "vllm", 8000, "hostname", "http://vllm:8000/v1"),
        (
            "http://VLLM.Internal:8000/v1/",
            "vllm.internal",
            8000,
            "domain",
            "http://vllm.internal:8000/v1",
        ),
        ("http://10.0.0.12:8000/v1", "10.0.0.12", 8000, "ipv4", "http://10.0.0.12:8000/v1"),
        ("http://[fd00::1]:8000/v1", "fd00::1", 8000, "ipv6", "http://[fd00::1]:8000/v1"),
        (
            "https://api.openai.com/v1",
            "api.openai.com",
            None,
            "domain",
            "https://api.openai.com/v1",
        ),
        ("http://gateway", "gateway", None, "hostname", "http://gateway"),
    ],
)
def test_accepted_shapes(url: str, host: str, port: int | None, kind: str, normalised: str) -> None:
    endpoint = parse_endpoint(url)
    assert (endpoint.host, endpoint.port, endpoint.kind) == (host, port, kind)
    assert endpoint.url == normalised
    assert endpoint.policy_host == host  # the bare host: no port, no brackets
    assert endpoint.insecure == url.startswith("http://")


def test_authority_restores_ipv6_brackets() -> None:
    assert parse_endpoint("http://[fd00::1]:8000/v1").authority == "[fd00::1]:8000"
    assert parse_endpoint("https://models.example/v1").authority == "models.example"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "empty"),
        ("vllm.internal:8000/v1", "must start with http:// or https://"),
        ("ftp://vllm.internal/v1", "must start with http:// or https://"),
        ("https://user:pw@vllm.internal/v1", "carries a credential in the URL"),
        ("https://vllm.internal/v1?x=1", "must not carry a query or fragment"),
        ("https://vllm.internal/v1#frag", "must not carry a query or fragment"),
        ("https:///v1", "names no host"),
        ("http://vllm.internal:99999/v1", "invalid port"),
        ("http://vllm.internal:0/v1", "port outside 1-65535"),
        ("http://bad_host/v1", "not a domain, a hostname, an IPv4 or an IPv6 literal"),
        ("http://-vllm/v1", "not a domain, a hostname, an IPv4 or an IPv6 literal"),
    ],
)
def test_refused_shapes_name_the_reason(url: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_endpoint(url)


def test_host_kind_classifies_and_rejects() -> None:
    assert host_kind("models.example.com") == "domain"
    assert host_kind("vllm") == "hostname"
    assert host_kind("10.0.0.12") == "ipv4"
    assert host_kind("fd00::1") == "ipv6"
    assert host_kind("") is None
    assert host_kind("*.example.com") is None
    assert host_kind("bad_host") is None
