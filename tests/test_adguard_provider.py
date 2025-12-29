"""Unit tests for AdGuardDNSProvider."""

from unittest.mock import MagicMock, patch

import requests

from external_dns.cli import AdGuardDNSProvider, DNSRecord


class TestAdGuardConnection:
    """Tests for AdGuard connection functionality."""

    def test_test_connection_success(self) -> None:
        """Test successful connection returns True."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            result = provider.test_connection()

            assert result is True
            mock_get.assert_called_once_with("http://adguard.local/control/status", timeout=5)

    def test_test_connection_failure(self) -> None:
        """Test connection failure returns False and logs error."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

            result = provider.test_connection()

            assert result is False


class TestAdGuardGetRecords:
    """Tests for AdGuard get_records functionality."""

    def test_get_records_returns_dns_records(self) -> None:
        """Test get_records returns list of DNSRecord from JSON response."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        mock_response_data = [
            {"domain": "app.example.com", "answer": "10.0.0.1"},
            {"domain": "api.example.com", "answer": "10.0.0.2"},
        ]

        with patch.object(provider._session, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_response_data
            mock_get.return_value = mock_response

            records = provider.get_records()

            assert len(records) == 2
            assert records[0] == DNSRecord(domain="app.example.com", answer="10.0.0.1")
            assert records[1] == DNSRecord(domain="api.example.com", answer="10.0.0.2")
            mock_get.assert_called_once_with("http://adguard.local/control/rewrite/list", timeout=5)

    def test_get_records_returns_empty_on_error(self) -> None:
        """Test get_records returns empty list on error."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "get") as mock_get:
            mock_get.side_effect = requests.exceptions.RequestException("Network error")

            records = provider.get_records()

            assert records == []


class TestAdGuardAddRecord:
    """Tests for AdGuard add_record functionality."""

    def test_add_record_success(self) -> None:
        """Test add_record returns True on success."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            result = provider.add_record("app.example.com", "10.0.0.1")

            assert result is True
            mock_post.assert_called_once_with(
                "http://adguard.local/control/rewrite/add",
                json={"domain": "app.example.com", "answer": "10.0.0.1"},
                timeout=5,
            )

    def test_add_record_failure(self) -> None:
        """Test add_record returns False on error."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "post") as mock_post:
            mock_post.side_effect = requests.exceptions.RequestException("Server error")

            result = provider.add_record("app.example.com", "10.0.0.1")

            assert result is False


class TestAdGuardDeleteRecord:
    """Tests for AdGuard delete_record functionality."""

    def test_delete_record_success(self) -> None:
        """Test delete_record returns True on success."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            result = provider.delete_record("app.example.com", "10.0.0.1")

            assert result is True
            mock_post.assert_called_once_with(
                "http://adguard.local/control/rewrite/delete",
                json={"domain": "app.example.com", "answer": "10.0.0.1"},
                timeout=5,
            )

    def test_delete_record_failure(self) -> None:
        """Test delete_record returns False on error."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "post") as mock_post:
            mock_post.side_effect = requests.exceptions.RequestException("Server error")

            result = provider.delete_record("app.example.com", "10.0.0.1")

            assert result is False


class TestAdGuardUpdateRecord:
    """Tests for AdGuard update_record functionality."""

    def test_update_record_calls_delete_then_add(self) -> None:
        """Test update_record delegates to delete + add."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider, "delete_record", return_value=True) as mock_delete:
            with patch.object(provider, "add_record", return_value=True) as mock_add:
                result = provider.update_record("app.example.com", "10.0.0.1", "10.0.0.2")

                assert result is True
                mock_delete.assert_called_once_with("app.example.com", "10.0.0.1")
                mock_add.assert_called_once_with("app.example.com", "10.0.0.2")

    def test_update_record_returns_false_on_delete_failure(self) -> None:
        """Test update_record returns False if delete fails."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider, "delete_record", return_value=False) as mock_delete:
            with patch.object(provider, "add_record") as mock_add:
                result = provider.update_record("app.example.com", "10.0.0.1", "10.0.0.2")

                assert result is False
                mock_delete.assert_called_once()
                mock_add.assert_not_called()


class TestAdGuardAuthentication:
    """Tests for AdGuard authentication functionality."""

    def test_provider_uses_basic_auth_when_credentials_provided(self) -> None:
        """Test provider sets HTTPBasicAuth when credentials provided."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        assert provider._auth is not None
        assert provider._session.auth is not None
        assert provider._session.auth.username == "admin"  # type: ignore[union-attr]
        assert provider._session.auth.password == "secret"  # type: ignore[union-attr]

    def test_provider_works_without_auth(self) -> None:
        """Test provider has no auth when credentials not provided."""
        provider = AdGuardDNSProvider(url="http://adguard.local", username="", password="")

        assert provider._auth is None
        assert provider._session.auth is None


class TestAdGuardProviderName:
    """Tests for AdGuard provider name property."""

    def test_provider_name(self) -> None:
        """Test provider name returns expected value."""
        provider = AdGuardDNSProvider(url="http://adguard.local", username="", password="")

        assert provider.name == "AdGuard Home"


class TestAdGuardURLHandling:
    """Tests for AdGuard URL handling."""

    def test_url_trailing_slash_stripped(self) -> None:
        """Test trailing slash is stripped from URL."""
        provider = AdGuardDNSProvider(url="http://adguard.local/", username="", password="")

        assert provider._url == "http://adguard.local"


class TestAdGuardJSONErrorHandling:
    """Tests for AdGuard JSON error handling."""

    def test_get_records_handles_malformed_json_response(self) -> None:
        """Test get_records returns empty list on invalid JSON response."""
        import json

        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        with patch.object(provider._session, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.side_effect = json.JSONDecodeError("Invalid", "", 0)
            mock_get.return_value = mock_response

            records = provider.get_records()

            assert records == []

    def test_get_records_skips_malformed_records(self) -> None:
        """Test get_records continues parsing valid records when some are malformed."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        mock_response_data = [
            {"domain": "valid.example.com", "answer": "10.0.0.1"},
            "not_a_dict",  # Malformed: not a dict
            {"domain": "another.example.com", "answer": "10.0.0.2"},
            123,  # Malformed: not a dict
        ]

        with patch.object(provider._session, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_response_data
            mock_get.return_value = mock_response

            records = provider.get_records()

            assert len(records) == 2
            assert records[0] == DNSRecord(domain="valid.example.com", answer="10.0.0.1")
            assert records[1] == DNSRecord(domain="another.example.com", answer="10.0.0.2")

    def test_get_records_handles_missing_fields(self) -> None:
        """Test get_records skips records missing domain or answer fields."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        mock_response_data = [
            {"domain": "valid.example.com", "answer": "10.0.0.1"},
            {"domain": "missing_answer.example.com"},  # Missing answer
            {"answer": "10.0.0.2"},  # Missing domain
            {"domain": None, "answer": "10.0.0.3"},  # None domain
            {"domain": "null_answer.example.com", "answer": None},  # None answer
            {"domain": 123, "answer": "10.0.0.4"},  # Non-string domain
            {"domain": "nonstring_answer.example.com", "answer": 456},  # Non-string answer
            {},  # Empty dict
        ]

        with patch.object(provider._session, "get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_response_data
            mock_get.return_value = mock_response

            records = provider.get_records()

            assert len(records) == 1
            assert records[0] == DNSRecord(domain="valid.example.com", answer="10.0.0.1")


class TestAdGuardRetryBehavior:
    """Tests for AdGuard retry behavior on transient failures."""

    def test_test_connection_retries_on_transient_failure(self) -> None:
        """Test that test_connection retries on transient failure and succeeds."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        call_count = 0

        def mock_get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("Connection refused")
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            return mock_response

        with patch.object(provider._session, "get", side_effect=mock_get_side_effect):
            with patch("external_dns.cli.time.sleep"):  # Skip sleep delays
                result = provider.test_connection()

        assert result is True
        assert call_count == 2  # First failed, second succeeded

    def test_get_records_retries_on_transient_failure(self) -> None:
        """Test that get_records retries on transient failure and succeeds."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        call_count = 0
        mock_response_data = [{"domain": "app.example.com", "answer": "10.0.0.1"}]

        def mock_get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("Connection refused")
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_response_data
            return mock_response

        with patch.object(provider._session, "get", side_effect=mock_get_side_effect):
            with patch("external_dns.cli.time.sleep"):  # Skip sleep delays
                records = provider.get_records()

        assert len(records) == 1
        assert records[0] == DNSRecord(domain="app.example.com", answer="10.0.0.1")
        assert call_count == 2

    def test_add_record_retries_on_transient_failure(self) -> None:
        """Test that add_record retries on transient failure and succeeds."""
        provider = AdGuardDNSProvider(
            url="http://adguard.local", username="admin", password="secret"
        )

        call_count = 0

        def mock_post_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("Connection refused")
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            return mock_response

        with patch.object(provider._session, "post", side_effect=mock_post_side_effect):
            with patch("external_dns.cli.time.sleep"):  # Skip sleep delays
                result = provider.add_record("app.example.com", "10.0.0.1")

        assert result is True
        assert call_count == 2
