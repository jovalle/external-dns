"""Unit tests for webhook functionality in external_dns.cli.

Tests cover:
- WebhookConfig dataclass
- call_webhook function with retry logic
- Webhook configuration loading from YAML and env vars
- Integration with sync_once return value
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from external_dns.cli import (
    WebhookConfig,
    call_webhook,
    load_settings_from_yaml,
)

# =============================================================================
# WebhookConfig Tests
# =============================================================================


def test_webhook_config_defaults() -> None:
    """WebhookConfig has sensible defaults."""
    config = WebhookConfig()

    assert config.url == ""
    assert config.username == ""
    assert config.password == ""
    assert config.method == "POST"
    assert config.timeout == 30
    assert config.only_on_changes is True
    assert config.enabled is False


def test_webhook_config_enabled_when_url_set() -> None:
    """WebhookConfig.enabled is True when URL is provided."""
    config = WebhookConfig(url="http://example.com/sync")

    assert config.enabled is True


def test_webhook_config_disabled_when_url_empty() -> None:
    """WebhookConfig.enabled is False when URL is empty."""
    config = WebhookConfig(url="")

    assert config.enabled is False


def test_webhook_config_with_auth() -> None:
    """WebhookConfig can be configured with authentication."""
    config = WebhookConfig(
        url="http://example.com/api/v1/sync",
        username="admin",
        password="secret123",
        method="POST",
        timeout=60,
        only_on_changes=False,
    )

    assert config.url == "http://example.com/api/v1/sync"
    assert config.username == "admin"
    assert config.password == "secret123"
    assert config.method == "POST"
    assert config.timeout == 60
    assert config.only_on_changes is False
    assert config.enabled is True


# =============================================================================
# call_webhook Tests
# =============================================================================


def test_call_webhook_returns_false_when_url_empty() -> None:
    """call_webhook returns False when URL is empty."""
    result = call_webhook("")

    assert result is False


def test_call_webhook_success() -> None:
    """call_webhook returns True on successful HTTP response."""
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200

    with patch("external_dns.cli.requests.request", return_value=mock_response) as mock_request:
        result = call_webhook("http://example.com/sync")

    assert result is True
    mock_request.assert_called_once()
    call_args = mock_request.call_args
    assert call_args.kwargs["method"] == "POST"
    assert call_args.kwargs["url"] == "http://example.com/sync"


def test_call_webhook_with_auth() -> None:
    """call_webhook uses HTTP Basic Auth when credentials provided."""
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200

    with patch("external_dns.cli.requests.request", return_value=mock_response) as mock_request:
        result = call_webhook(
            "http://example.com/sync",
            username="admin",
            password="secret",
        )

    assert result is True
    call_args = mock_request.call_args
    assert call_args.kwargs["auth"] is not None


def test_call_webhook_without_auth() -> None:
    """call_webhook sends no auth when credentials not provided."""
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200

    with patch("external_dns.cli.requests.request", return_value=mock_response) as mock_request:
        result = call_webhook("http://example.com/sync")

    assert result is True
    call_args = mock_request.call_args
    assert call_args.kwargs["auth"] is None


def test_call_webhook_custom_method() -> None:
    """call_webhook uses the specified HTTP method."""
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200

    with patch("external_dns.cli.requests.request", return_value=mock_response) as mock_request:
        result = call_webhook("http://example.com/sync", method="GET")

    assert result is True
    call_args = mock_request.call_args
    assert call_args.kwargs["method"] == "GET"


def test_call_webhook_returns_false_on_error_status() -> None:
    """call_webhook returns False when HTTP response is not OK."""
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 500

    with patch("external_dns.cli.requests.request", return_value=mock_response):
        result = call_webhook("http://example.com/sync")

    assert result is False


def test_call_webhook_retries_on_failure() -> None:
    """call_webhook retries on connection errors."""
    call_count = 0

    def mock_request_func(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise requests.exceptions.ConnectionError("Connection refused")
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        return mock_response

    with patch("external_dns.cli.requests.request", side_effect=mock_request_func):
        with patch("external_dns.cli.time.sleep"):  # Skip actual sleep
            result = call_webhook("http://example.com/sync")

    assert result is True
    assert call_count == 3


def test_call_webhook_fails_after_max_retries() -> None:
    """call_webhook returns False after exhausting retries."""
    call_count = 0

    def mock_request_func(**kwargs):
        nonlocal call_count
        call_count += 1
        raise requests.exceptions.ConnectionError("Connection refused")

    with patch("external_dns.cli.requests.request", side_effect=mock_request_func):
        with patch("external_dns.cli.time.sleep"):  # Skip actual sleep
            result = call_webhook("http://example.com/sync", max_retries=2)

    assert result is False
    assert call_count == 3  # Initial + 2 retries


# =============================================================================
# Webhook YAML Configuration Tests
# =============================================================================


def test_load_webhook_from_yaml() -> None:
    """Webhook configuration is loaded from YAML config."""
    yaml_content = """
webhook:
  url: "http://adguard-sync:8080/api/v1/sync"
  username: "admin"
  password: "secret"
  method: "POST"
  timeout: 60
  only_on_changes: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        settings = load_settings_from_yaml(config_path)

        assert settings.webhook.enabled is True
        assert settings.webhook.url == "http://adguard-sync:8080/api/v1/sync"
        assert settings.webhook.username == "admin"
        assert settings.webhook.password == "secret"
        assert settings.webhook.method == "POST"
        assert settings.webhook.timeout == 60
        assert settings.webhook.only_on_changes is True
    finally:
        Path(config_path).unlink()


def test_load_webhook_from_yaml_defaults() -> None:
    """Webhook configuration uses defaults when not specified in YAML."""
    yaml_content = """
webhook:
  url: "http://example.com/sync"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        settings = load_settings_from_yaml(config_path)

        assert settings.webhook.enabled is True
        assert settings.webhook.url == "http://example.com/sync"
        assert settings.webhook.username == ""
        assert settings.webhook.password == ""
        assert settings.webhook.method == "POST"
        assert settings.webhook.timeout == 30
        assert settings.webhook.only_on_changes is True
    finally:
        Path(config_path).unlink()


def test_load_webhook_disabled_when_not_in_yaml() -> None:
    """Webhook is disabled when not configured in YAML."""
    yaml_content = """
settings:
  poll_interval: 30
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        settings = load_settings_from_yaml(config_path)

        assert settings.webhook.enabled is False
    finally:
        Path(config_path).unlink()


# =============================================================================
# Webhook Environment Variable Tests
# =============================================================================


def test_webhook_env_vars_override_yaml() -> None:
    """Environment variables override YAML webhook config."""
    yaml_content = """
webhook:
  url: "http://yaml-url.com/sync"
  username: "yaml-user"
  password: "yaml-pass"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        env_vars = {
            "EXTERNAL_DNS_WEBHOOK_URL": "http://env-url.com/sync",
            "EXTERNAL_DNS_WEBHOOK_USERNAME": "env-user",
            "EXTERNAL_DNS_WEBHOOK_PASSWORD": "env-pass",
            "EXTERNAL_DNS_WEBHOOK_METHOD": "GET",
            "EXTERNAL_DNS_WEBHOOK_TIMEOUT": "120",
            "EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES": "false",
        }
        with patch.dict("os.environ", env_vars, clear=False):
            settings = load_settings_from_yaml(config_path)

        assert settings.webhook.url == "http://env-url.com/sync"
        assert settings.webhook.username == "env-user"
        assert settings.webhook.password == "env-pass"
        assert settings.webhook.method == "GET"
        assert settings.webhook.timeout == 120
        assert settings.webhook.only_on_changes is False
    finally:
        Path(config_path).unlink()


def test_webhook_env_var_only_on_changes_variations() -> None:
    """only_on_changes env var accepts various true/false values."""
    yaml_content = """
webhook:
  url: "http://example.com/sync"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        # Test "true" values
        for true_val in ["true", "True", "TRUE", "1", "yes", "Yes"]:
            with patch.dict("os.environ", {"EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES": true_val}):
                settings = load_settings_from_yaml(config_path)
                assert settings.webhook.only_on_changes is True, f"Failed for {true_val}"

        # Test "false" values
        for false_val in ["false", "False", "FALSE", "0", "no", "No", "anything"]:
            with patch.dict("os.environ", {"EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES": false_val}):
                settings = load_settings_from_yaml(config_path)
                if false_val in ("true", "1", "yes"):
                    assert settings.webhook.only_on_changes is True
                else:
                    assert settings.webhook.only_on_changes is False, f"Failed for {false_val}"
    finally:
        Path(config_path).unlink()


def test_webhook_env_vars_without_yaml() -> None:
    """Webhook can be configured via env vars alone without YAML."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("# Empty config\n")
        config_path = f.name

    try:
        env_vars = {
            "EXTERNAL_DNS_WEBHOOK_URL": "http://env-only.com/sync",
            "EXTERNAL_DNS_WEBHOOK_USERNAME": "envuser",
            "EXTERNAL_DNS_WEBHOOK_PASSWORD": "envpass",
        }
        with patch.dict("os.environ", env_vars, clear=False):
            settings = load_settings_from_yaml(config_path)

        assert settings.webhook.enabled is True
        assert settings.webhook.url == "http://env-only.com/sync"
        assert settings.webhook.username == "envuser"
        assert settings.webhook.password == "envpass"
    finally:
        Path(config_path).unlink()
