from __future__ import annotations

import pytest
from oauth2_provider.cimd import CIMDError, SafeMetadataFetcher

from codito_relay.core.oauth import CoditoCIMDMetadataFetcher


def test_cimd_fetcher_uses_explicit_none_fallback(monkeypatch) -> None:
    original = {
        "client_id": "https://chatgpt.com/.well-known/oauth-client",
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"],
        "redirect_uris": ["https://chatgpt.com/aip/plugin/oauth/callback"],
    }
    monkeypatch.setattr(SafeMetadataFetcher, "fetch", lambda _self, _client_id: (original, 300))

    metadata, max_age = CoditoCIMDMetadataFetcher().fetch(original["client_id"])

    assert metadata["token_endpoint_auth_method"] == "none"  # noqa: S105 - OAuth enum.
    assert max_age == 300
    assert original["token_endpoint_auth_method"] == "private_key_jwt"  # noqa: S105


@pytest.mark.parametrize(
    "supported",
    [
        ["private_key_jwt"],
        "none",
        None,
    ],
)
def test_cimd_fetcher_never_downgrades_without_explicit_none(monkeypatch, supported) -> None:  # type: ignore[no-untyped-def]
    metadata = {
        "client_id": "https://chatgpt.com/.well-known/oauth-client",
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": supported,
        "redirect_uris": ["https://chatgpt.com/aip/plugin/oauth/callback"],
    }
    monkeypatch.setattr(SafeMetadataFetcher, "fetch", lambda _self, _client_id: (metadata, 300))

    with pytest.raises(CIMDError, match="does not explicitly"):
        CoditoCIMDMetadataFetcher().fetch(metadata["client_id"])


def test_cimd_fetcher_refuses_unapproved_host_before_network(monkeypatch) -> None:
    called = False

    def fake_fetch(_self, _client_id):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        return {}, 300

    monkeypatch.setattr(SafeMetadataFetcher, "fetch", fake_fetch)
    with pytest.raises(CIMDError, match="not approved"):
        CoditoCIMDMetadataFetcher().fetch("https://attacker.example/client.json")
    assert called is False
