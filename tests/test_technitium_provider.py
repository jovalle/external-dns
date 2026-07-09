"""Unit tests for TechnitiumDNSProvider."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from external_dns import cli
from external_dns.cli import (
    DNSProviderReadError,
    DNSRecord,
    TechnitiumDNSProvider,
    load_dns_providers_from_yaml,
)


def test_factory_creates_technitium_provider(monkeypatch, tmp_path: Path) -> None:
    """DNS_PROVIDER=technitium creates a Technitium provider from env fallback config."""
    monkeypatch.setattr(cli, "DNS_PROVIDER", "technitium")
    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(cli, "TECHNITIUM_URL", "http://technitium.local")
    monkeypatch.setattr(cli, "TECHNITIUM_API_TOKEN", "secret-token")
    monkeypatch.setattr(cli, "TECHNITIUM_ZONES", "example.com, internal.example.com")

    provider = cli.create_dns_provider()

    assert isinstance(provider, TechnitiumDNSProvider)
    assert provider._url == "http://technitium.local"
    assert provider._zones == ["example.com", "internal.example.com"]


def test_provider_uses_bearer_token() -> None:
    """Technitium auth uses an Authorization bearer token, not username/password login."""
    provider = TechnitiumDNSProvider(
        url="http://technitium.local/",
        api_token="secret-token",
        zones=["example.com"],
    )

    assert provider._session.headers["Authorization"] == "Bearer secret-token"
    assert provider._session.auth is None


def test_load_dns_providers_from_yaml_includes_technitium_zones(tmp_path: Path) -> None:
    """YAML providers entries carry normalized Technitium zones into config."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
providers:
  - name: authoritative
    provider: technitium
    url: http://technitium.local
    api_token: secret-token
    zones:
      - Example.COM.
      - " internal.example.com "
      - ""
""",
        encoding="utf-8",
    )

    providers = load_dns_providers_from_yaml(str(config_path))

    assert len(providers) == 1
    assert providers[0].provider == "technitium"
    assert providers[0].api_token == "secret-token"
    assert providers[0].zones == ["example.com", "internal.example.com"]


def _json_response(data, json_side_effect=None):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    if json_side_effect is None:
        response.json.return_value = data
    else:
        response.json.side_effect = json_side_effect
    return response


def test_test_connection_uses_status_endpoint() -> None:
    """Connection test validates bearer auth with the Technitium status endpoint."""
    provider = TechnitiumDNSProvider(
        url="http://technitium.local",
        api_token="secret-token",
        zones=["example.com"],
    )

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response({"status": "ok"})

        assert provider.test_connection() is True

        mock_get.assert_called_once_with("http://technitium.local/api/status", timeout=5)


def test_get_records_lists_a_records_from_zones() -> None:
    """Configured zones are listed explicitly and filtered to enabled A records."""
    provider = TechnitiumDNSProvider(
        url="http://technitium.local",
        api_token="secret-token",
        zones=["example.com", "internal.example.com"],
    )

    first_zone = {
        "status": "ok",
        "response": {
            "records": [
                {
                    "disabled": False,
                    "name": "app.example.com",
                    "type": "A",
                    "rData": {"ipAddress": "10.0.0.10"},
                },
                {
                    "disabled": True,
                    "name": "disabled.example.com",
                    "type": "A",
                    "rData": {"ipAddress": "10.0.0.11"},
                },
                {
                    "disabled": False,
                    "name": "alias.example.com",
                    "type": "CNAME",
                    "rData": {"cname": "app.example.com"},
                },
                {
                    "disabled": False,
                    "name": "other.test",
                    "type": "A",
                    "rData": {"ipAddress": "10.0.0.12"},
                },
                {
                    "disabled": False,
                    "name": "bad-ip.example.com",
                    "type": "A",
                    "rData": {"ipAddress": 42},
                },
                {"name": "missing-fields.example.com", "type": "A"},
            ]
        },
    }
    second_zone = {
        "status": "ok",
        "response": {
            "records": [
                {
                    "disabled": False,
                    "name": "api.internal.example.com",
                    "type": "A",
                    "rData": {"ipAddress": "10.0.0.20"},
                }
            ]
        },
    }

    with patch.object(provider._session, "get") as mock_get:
        mock_get.side_effect = [_json_response(first_zone), _json_response(second_zone)]

        records = provider.get_records()

    assert records == [
        DNSRecord(domain="app.example.com", answer="10.0.0.10"),
        DNSRecord(domain="api.internal.example.com", answer="10.0.0.20"),
    ]
    assert mock_get.call_args_list == [
        call(
            "http://technitium.local/api/zones/records/get",
            params={"domain": "example.com", "zone": "example.com", "listZone": "true"},
            timeout=5,
        ),
        call(
            "http://technitium.local/api/zones/records/get",
            params={
                "domain": "internal.example.com",
                "zone": "internal.example.com",
                "listZone": "true",
            },
            timeout=5,
        ),
    ]


def test_add_delete_update_records() -> None:
    """A-record writes use Technitium endpoints and the longest matching configured zone."""
    provider = TechnitiumDNSProvider(
        url="http://technitium.local",
        api_token="secret-token",
        zones=["example.com", "sub.example.com"],
    )

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response({"status": "ok"})

        assert provider.add_record("app.sub.example.com", "10.0.0.1") is True
        assert provider.delete_record("app.sub.example.com", "10.0.0.1") is True
        assert provider.update_record("app.sub.example.com", "10.0.0.1", "10.0.0.2") is True

    assert mock_get.call_args_list == [
        call(
            "http://technitium.local/api/zones/records/add",
            params={
                "domain": "app.sub.example.com",
                "zone": "sub.example.com",
                "type": "A",
                "ipAddress": "10.0.0.1",
            },
            timeout=5,
        ),
        call(
            "http://technitium.local/api/zones/records/delete",
            params={
                "domain": "app.sub.example.com",
                "zone": "sub.example.com",
                "type": "A",
                "ipAddress": "10.0.0.1",
            },
            timeout=5,
        ),
        call(
            "http://technitium.local/api/zones/records/update",
            params={
                "domain": "app.sub.example.com",
                "zone": "sub.example.com",
                "type": "A",
                "ipAddress": "10.0.0.1",
                "newIpAddress": "10.0.0.2",
            },
            timeout=5,
        ),
    ]


def test_api_status_errors_are_recoverable(caplog) -> None:
    """Technitium API, JSON, zone, and network failures return false/empty safely."""
    provider = TechnitiumDNSProvider(
        url="http://technitium.local",
        api_token="secret-token",
        zones=["example.com"],
    )

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response({"status": "error", "errorMessage": "denied"})
        assert provider.add_record("app.example.com", "10.0.0.1") is False

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response({"status": "invalid-token"})
        assert provider.delete_record("app.example.com", "10.0.0.1") is False

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response(
            None,
            json.JSONDecodeError("Invalid", "", 0),
        )
        assert provider.update_record("app.example.com", "10.0.0.1", "10.0.0.2") is False

    with patch.object(provider._session, "get") as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError("network down")
        with patch("external_dns.cli.time.sleep"):
            assert provider.add_record("app.example.com", "10.0.0.1") is False
        assert mock_get.call_count == 3

    with patch.object(provider._session, "get") as mock_get:
        assert provider.add_record("outside.test", "10.0.0.1") is False
        mock_get.assert_not_called()

    with patch.object(provider._session, "get") as mock_get:
        mock_get.return_value = _json_response({"status": "invalid-token"})
        with pytest.raises(DNSProviderReadError):
            provider.get_records()

    assert "secret-token" not in caplog.text
