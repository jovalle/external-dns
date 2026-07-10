"""Unit tests for GokuProvider."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from external_dns.cli import DNSProviderReadError, DNSRecord, GokuProvider


def test_provider_uses_bearer_token() -> None:
    """Goku API token is sent as bearer auth."""
    provider = GokuProvider("http://goku.local/", api_token="secret-token")

    assert provider._url == "http://goku.local"
    assert provider._session.headers["Authorization"] == "Bearer secret-token"


def test_test_connection_uses_aliases_endpoint() -> None:
    """Connection test checks the aliases API."""
    provider = GokuProvider("http://goku.local", api_token="secret-token")

    with patch.object(provider._session, "get") as mock_get:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert provider.test_connection() is True

    mock_get.assert_called_once_with("http://goku.local/api/aliases", timeout=5)


def test_get_records_maps_aliases_to_dns_record_shape() -> None:
    """Goku aliases are exposed as alias -> destination records."""
    provider = GokuProvider("http://goku.local")

    with patch.object(provider._session, "get") as mock_get:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"alias": "immich", "destination": "https://photos.example.com"},
            {"alias": "traefik", "destination": "https://traefik.example.com", "enabled": False},
        ]
        mock_get.return_value = mock_response

        records = provider.get_records()

    assert records == [
        DNSRecord("immich", "https://photos.example.com"),
        DNSRecord("traefik", "https://traefik.example.com"),
    ]


def test_get_records_raises_on_malformed_response() -> None:
    """Non-list Goku responses are treated as unsafe reads."""
    provider = GokuProvider("http://goku.local")

    with patch.object(provider._session, "get") as mock_get:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"aliases": []}
        mock_get.return_value = mock_response

        with pytest.raises(DNSProviderReadError):
            provider.get_records()


def test_add_record_posts_alias_form() -> None:
    """Adding a record upserts a Goku alias."""
    provider = GokuProvider("http://goku.local")

    with patch.object(provider._session, "post") as mock_post:
        mock_response = MagicMock(status_code=303)
        mock_post.return_value = mock_response

        assert provider.add_record("immich", "https://photos.example.com") is True

    mock_post.assert_called_once_with(
        "http://goku.local/api/aliases",
        data={"alias": "immich", "destination": "https://photos.example.com"},
        timeout=5,
        allow_redirects=False,
    )


def test_delete_record_posts_alias_delete_form() -> None:
    """Deleting a record removes a Goku alias by alias name."""
    provider = GokuProvider("http://goku.local")

    with patch.object(provider._session, "post") as mock_post:
        mock_response = MagicMock(status_code=303)
        mock_post.return_value = mock_response

        assert provider.delete_record("immich", "https://photos.example.com") is True

    mock_post.assert_called_once_with(
        "http://goku.local/api/aliases/delete",
        data={"alias": "immich"},
        timeout=5,
        allow_redirects=False,
    )


def test_add_record_returns_false_on_request_error() -> None:
    """Write errors are recoverable."""
    provider = GokuProvider("http://goku.local")

    with patch.object(provider._session, "post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")

        assert provider.add_record("immich", "https://photos.example.com") is False


def test_goku_projection_skips_static_and_uses_golink_metadata() -> None:
    """Goku provider projects source metadata to alias destination pairs."""
    provider = GokuProvider("http://goku.local")

    assert provider.desired_static_record("static.example.com", "10.0.0.1") is None
    assert provider.desired_source_record(
        "photos.example.com",
        {
            "golink_alias": "immich",
            "golink_destination": "https://photos.example.com",
            "golink_enabled": True,
        },
    ) == DNSRecord("immich", "https://photos.example.com")
    assert (
        provider.desired_source_record(
            "photos.example.com",
            {
                "golink_alias": "immich",
                "golink_destination": "https://photos.example.com",
                "golink_enabled": False,
            },
        )
        is None
    )


def test_goku_projection_uses_multiple_golink_aliases() -> None:
    """Goku provider can project one route into multiple aliases."""
    provider = GokuProvider("http://goku.local")

    assert provider.desired_source_records(
        "stat.example.com",
        {
            "golink_alias": "kromgo",
            "golink_aliases": ["kromgo", "stat", "kromgo"],
            "golink_destination": "https://stat.example.com",
            "golink_enabled": True,
        },
    ) == [
        DNSRecord("kromgo", "https://stat.example.com"),
        DNSRecord("stat", "https://stat.example.com"),
    ]
