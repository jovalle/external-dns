"""Unit tests for TraefikProxyProvider."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from external_dns.cli import DNSZone, ProxyInstance, RouteSourceReadError, TraefikProxyProvider


class TestTraefikInstanceLoadingFromYaml:
    """Tests for Traefik instance loading from YAML config."""

    def test_get_instances_from_yaml_config(self, tmp_path: Path) -> None:
        """Test loading instances from YAML config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
sources:
  - name: core
    url: http://traefik:8080
    target_ip: 10.0.0.2
    verify_tls: true
    router_filter: "*-internal"
  - name: edge
    url: https://traefik2:8080
    target_ip: 10.0.0.3
    public_target_ip: "  203.0.113.10  "
    verify_tls: false
""")

        provider = TraefikProxyProvider(config_path=str(config_file))
        instances = provider.get_instances()

        assert len(instances) == 2
        assert instances[0].name == "core"
        assert instances[0].url == "http://traefik:8080"
        assert instances[0].target_ip == "10.0.0.2"
        assert instances[0].public_target_ip == ""
        assert instances[0].verify_tls is True
        assert instances[0].router_filter == "*-internal"
        assert instances[1].name == "edge"
        assert instances[1].url == "https://traefik2:8080"
        assert instances[1].target_ip == "10.0.0.3"
        assert instances[1].public_target_ip == "203.0.113.10"
        assert instances[1].verify_tls is False

    def test_get_instances_skips_invalid_entries(self, tmp_path: Path) -> None:
        """Test that entries with missing required fields are skipped."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
sources:
  - name: valid
    url: http://traefik:8080
    target_ip: 10.0.0.2
  - name: missing_url
    target_ip: 10.0.0.3
  - name: missing_ip
    url: http://traefik2:8080
  - not_a_dict
""")

        provider = TraefikProxyProvider(config_path=str(config_file))
        instances = provider.get_instances()

        assert len(instances) == 1
        assert instances[0].name == "valid"

    def test_invalid_yaml_sources_do_not_fallback_to_json_env(self, tmp_path: Path) -> None:
        """A configured YAML sources section takes precedence even when invalid."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
sources:
  - name: missing_url
    target_ip: 10.0.0.3
""")
        json_config = json.dumps(
            [{"name": "env", "url": "http://traefik-env:8080", "target_ip": "10.0.0.9"}]
        )

        provider = TraefikProxyProvider(
            config_path=str(config_file),
            instances_json=json_config,
        )
        instances = provider.get_instances()

        assert instances == []


class TestTraefikInstanceLoadingFromJson:
    """Tests for Traefik instance loading from JSON environment variable."""

    def test_get_instances_from_json_env(self) -> None:
        """Test loading instances from JSON string."""
        json_config = json.dumps(
            [
                {"name": "core", "url": "http://traefik:8080", "target_ip": "10.0.0.2"},
                {
                    "name": "edge",
                    "url": "https://traefik2:8080",
                    "target_ip": "10.0.0.3",
                    "public_target_ip": "  203.0.113.20  ",
                    "verify_tls": False,
                },
            ]
        )

        provider = TraefikProxyProvider(
            config_path="/nonexistent/path.yaml",  # Ensure YAML path doesn't exist
            instances_json=json_config,
        )
        instances = provider.get_instances()

        assert len(instances) == 2
        assert instances[0].name == "core"
        assert instances[0].url == "http://traefik:8080"
        assert instances[0].public_target_ip == ""
        assert instances[1].name == "edge"
        assert instances[1].public_target_ip == "203.0.113.20"
        assert instances[1].verify_tls is False


class TestTraefikInstanceSingleFallback:
    """Tests for Traefik single-instance fallback mode."""

    def test_get_instances_single_fallback(self) -> None:
        """Test single-instance fallback using URL and target_ip."""
        provider = TraefikProxyProvider(
            config_path="/nonexistent/path.yaml",
            instances_json="",
            url="http://traefik:8080",
            target_ip="10.0.0.2",
        )
        instances = provider.get_instances()

        assert len(instances) == 1
        assert instances[0].name == "traefik"
        assert instances[0].url == "http://traefik:8080"
        assert instances[0].target_ip == "10.0.0.2"

    def test_get_instances_empty_when_no_config(self) -> None:
        """Test empty list returned when no config is available."""
        provider = TraefikProxyProvider(
            config_path="/nonexistent/path.yaml",
            instances_json="",
            url="",
            target_ip="",
        )
        instances = provider.get_instances()

        assert instances == []


class TestTraefikRouteDiscovery:
    """Tests for Traefik route discovery from API."""

    def test_get_routes_extracts_hostnames_from_host_rule(self) -> None:
        """Test extracting hostname from Host() rule."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "app@docker", "rule": "Host(`app.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].hostname == "app.example.com"
            assert routes[0].source_name == "test"
            assert routes[0].target_ip == "10.0.0.1"
            assert routes[0].golink_alias == "app"
            assert routes[0].golink_destination == "https://app.example.com"

    def test_get_routes_handles_multiple_routers(self) -> None:
        """Test extracting hostnames from multiple routers."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "app1@docker", "rule": "Host(`app1.example.com`)"},
            {"name": "app2@docker", "rule": "Host(`app2.example.com`)"},
            {"name": "app3@docker", "rule": "Host(`app3.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 3
            hostnames = {r.hostname for r in routes}
            assert hostnames == {"app1.example.com", "app2.example.com", "app3.example.com"}

    def test_get_routes_prefers_service_name_for_golink_alias(self) -> None:
        """GoLink alias comes from service identity while destination is the FQDN."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "websecure-immich@docker",
                "service": "immich@docker",
                "rule": "Host(`photos.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_alias == "immich"
            assert routes[0].golink_aliases == ["immich", "photos"]
            assert routes[0].golink_destination == "https://photos.example.com"

    def test_get_routes_adds_service_and_hostname_golink_aliases(self) -> None:
        """Different service and hostname basenames both become GoLink aliases."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="nexus", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "websecure-kromgo@docker",
                "service": "10-kromgo-service-nexus@docker",
                "rule": "Host(`stat.example.com`)",
            },
            {
                "name": "websecure-garage-s3@docker",
                "service": "garage-s3@docker",
                "rule": "Host(`s3.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            aliases_by_hostname = {route.hostname: route.golink_aliases for route in routes}
            assert aliases_by_hostname["stat.example.com"] == ["kromgo", "stat"]
            assert aliases_by_hostname["s3.example.com"] == ["garage-s3", "s3"]

    def test_get_routes_strips_source_stack_suffix_from_service_alias(self) -> None:
        """Unique services do not inherit source or stack suffixes in GoLink aliases."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="nexus", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "websecure-dozzle@docker",
                "service": "dozzle-nexus@docker",
                "rule": "Host(`dozzle.example.com`)",
                "labels": {"com.docker.compose.project": "nexus"},
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_aliases == ["dozzle"]

    def test_get_routes_collapses_truenas_ix_service_alias(self) -> None:
        """TrueNAS ix generated service names do not become duplicate GoLink aliases."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="nexus", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "websecure-scrutiny@docker",
                "service": "scrutiny-ix-scrutiny@docker",
                "rule": "Host(`scrutiny.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_aliases == ["scrutiny"]
            assert routes[0].golink_destination == "https://scrutiny.example.com"

    def test_get_routes_strips_entrypoint_prefix_from_router_fallback(self) -> None:
        """Router fallback removes common entrypoint prefixes when service is unavailable."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "websecure-sonarr@docker", "rule": "Host(`sonarr.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_alias == "sonarr"

    def test_get_routes_keeps_one_golink_for_router_with_multiple_hostnames(self) -> None:
        """One router can expose many DNS names without creating GoLink alias conflicts."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "websecure-technitium@docker",
                "service": "technitium@docker",
                "rule": "Host(`ns1.example.com`, `technitium.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert {route.hostname for route in routes} == {
                "ns1.example.com",
                "technitium.example.com",
            }
            enabled_routes = [route for route in routes if route.golink_enabled]
            assert len(enabled_routes) == 2
            aliases = {
                alias: route.golink_destination
                for route in enabled_routes
                for alias in route.golink_aliases
            }
            assert aliases == {
                "ns1": "https://ns1.example.com",
                "technitium": "https://technitium.example.com",
            }

    def test_get_routes_keeps_one_golink_for_service_with_multiple_routers(self) -> None:
        """One service can have many routers without publishing many GoLink destinations."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "web-sonarr@docker",
                "service": "sonarr@docker",
                "rule": "Host(`sonarr.local.example.com`)",
            },
            {
                "name": "websecure-sonarr@docker",
                "service": "sonarr@docker",
                "rule": "Host(`sonarr.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert {route.hostname for route in routes} == {
                "sonarr.local.example.com",
                "sonarr.example.com",
            }
            enabled_routes = [route for route in routes if route.golink_enabled]
            assert len(enabled_routes) == 1
            assert enabled_routes[0].golink_alias == "sonarr"
            assert enabled_routes[0].golink_destination == "https://sonarr.example.com"

    def test_get_routes_uses_golink_alias_template(self) -> None:
        """Source alias templates namespace overlapping app names."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(
            name="nexus",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
            golink_alias_template="{app}-{source}",
        )

        mock_routers = [
            {"name": "traefik-internal@docker", "rule": "Host(`traefik.nexus.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_alias == "traefik-nexus"
            assert routes[0].golink_destination == "https://traefik.nexus.example.com"

    def test_get_routes_uses_explicit_golink_label_when_present(self) -> None:
        """Explicit GoLink labels override router-name derivation when exposed."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "photos@docker",
                "rule": "Host(`photos.example.com`)",
                "labels": {"external-dns.golink.alias": "immich"},
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_alias == "immich"

    def test_get_routes_disables_golink_with_middleware(self) -> None:
        """Configured opt-out middleware disables only GoLink publication metadata."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {
                "name": "admin@docker",
                "rule": "Host(`admin.example.com`)",
                "middlewares": ["no-golink@docker"],
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].golink_enabled is False

    def test_get_routes_leaves_connection_error_logging_to_caller(self, caplog) -> None:
        """The orchestration boundary logs a failed source exactly once."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        with patch("requests.Session.get") as mock_get, patch("time.sleep"):
            mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

            with pytest.raises(requests.exceptions.RequestException):
                provider.get_routes(instance)

        assert "Failed to get routes" not in caplog.text


class TestTraefikRouterFilter:
    """Tests for Traefik router filtering."""

    def test_router_filter_matches_wildcard_pattern(self) -> None:
        """Test router filter matches wildcard pattern."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(
            name="test",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
            router_filter="*-internal*",
        )

        mock_routers = [
            {"name": "app-internal@docker", "rule": "Host(`app.internal.example.com`)"},
            {"name": "api-internal@docker", "rule": "Host(`api.internal.example.com`)"},
            {"name": "public@docker", "rule": "Host(`public.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 2
            hostnames = {r.hostname for r in routes}
            assert hostnames == {"app.internal.example.com", "api.internal.example.com"}

    def test_router_filter_empty_matches_all(self) -> None:
        """Test empty router filter matches all routers."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(
            name="test",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
            router_filter="",
        )

        mock_routers = [
            {"name": "app-internal@docker", "rule": "Host(`app.internal.example.com`)"},
            {"name": "public@docker", "rule": "Host(`public.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 2


class TestTraefikMiddlewareFilter:
    """Tests for Traefik middleware filtering."""

    def test_middleware_filter_excludes_routers_without_middleware(self) -> None:
        """Test middleware filter excludes routers without the specified middleware."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(
            name="test",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
            middleware_filter="auth",
        )

        mock_routers = [
            {
                "name": "app-with-auth@docker",
                "rule": "Host(`app.example.com`)",
                "middlewares": ["auth@docker", "ratelimit@docker"],
            },
            {
                "name": "public@docker",
                "rule": "Host(`public.example.com`)",
                "middlewares": ["ratelimit@docker"],
            },
            {
                "name": "noauth@docker",
                "rule": "Host(`noauth.example.com`)",
            },
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].hostname == "app.example.com"


class TestTraefikZoneDetection:
    """Tests for Traefik zone detection."""

    def test_detect_zone_from_router_name_suffix_internal(self) -> None:
        """Test detecting INTERNAL zone from router name suffix."""
        provider = TraefikProxyProvider(default_zone="external")
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "myapp-internal@docker", "rule": "Host(`myapp.local.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.INTERNAL

    def test_detect_zone_from_router_name_suffix_external(self) -> None:
        """Test detecting EXTERNAL zone from router name suffix."""
        provider = TraefikProxyProvider(default_zone="internal")
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "myapp-external@docker", "rule": "Host(`myapp.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.EXTERNAL

    def test_detect_zone_defaults_when_no_suffix(self) -> None:
        """Test default zone is used when no suffix is present."""
        provider = TraefikProxyProvider(default_zone="internal")
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "myapp@docker", "rule": "Host(`myapp.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.INTERNAL

    def test_detect_zone_defaults_to_external_when_configured(self) -> None:
        """Test default zone is EXTERNAL when configured."""
        provider = TraefikProxyProvider(default_zone="external")
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "myapp@docker", "rule": "Host(`myapp.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.EXTERNAL

    def test_external_route_with_public_target_ip_uses_public_answer(self) -> None:
        """External routes from public-configured sources use explicit public IP."""
        provider = TraefikProxyProvider(default_zone="internal")
        instance = ProxyInstance(
            name="edge",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
            public_target_ip="203.0.113.30",
        )

        mock_routers = [
            {"name": "public-app-external@docker", "rule": "Host(`app.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.EXTERNAL
            assert routes[0].target_ip == "203.0.113.30"
            assert routes[0].publish_external is True

    def test_external_route_without_public_target_ip_keeps_internal_answer(self) -> None:
        """External routes without public_target_ip keep default skip intent."""
        provider = TraefikProxyProvider(default_zone="internal")
        instance = ProxyInstance(
            name="edge",
            url="http://traefik:8080",
            target_ip="10.0.0.1",
        )

        mock_routers = [
            {"name": "public-app-external@docker", "rule": "Host(`app.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            assert len(routes) == 1
            assert routes[0].zone == DNSZone.EXTERNAL
            assert routes[0].target_ip == "10.0.0.1"
            assert routes[0].publish_external is False


class TestTraefikProviderName:
    """Tests for Traefik provider name property."""

    def test_provider_name(self) -> None:
        """Test provider name returns expected value."""
        provider = TraefikProxyProvider()
        assert provider.name == "Traefik"


class TestTraefikHostnameExtraction:
    """Tests for Traefik hostname extraction from rules."""

    def test_extract_hostnames_from_backtick_rule(self) -> None:
        """Test extracting hostname from Host() with backticks."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames("Host(`example.com`)")
        assert hostnames == ["example.com"]

    def test_extract_hostnames_from_double_quote_rule(self) -> None:
        """Test extracting hostname from Host() with double quotes."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames('Host("example.com")')
        assert hostnames == ["example.com"]

    def test_extract_hostnames_from_single_quote_rule(self) -> None:
        """Test extracting hostname from Host() with single quotes."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames("Host('example.com')")
        assert hostnames == ["example.com"]

    def test_extract_hostnames_multiple_hosts(self) -> None:
        """Test extracting multiple hostnames from complex rule."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames(
            "Host(`app1.example.com`) || Host(`app2.example.com`)"
        )
        assert sorted(hostnames) == ["app1.example.com", "app2.example.com"]

    def test_extract_hostnames_multiple_args_in_single_host_call(self) -> None:
        """Test extracting multiple hostnames from one Host() call."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames("Host(`app1.example.com`, `app2.example.com`)")
        assert hostnames == ["app1.example.com", "app2.example.com"]

    def test_extract_hostnames_empty_rule(self) -> None:
        """Test extracting from empty rule returns empty list."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames("")
        assert hostnames == []

    def test_extract_hostnames_rejects_path_like_values(self) -> None:
        """Host values containing paths are not valid DNS hostnames."""
        provider = TraefikProxyProvider()
        hostnames = provider._extract_hostnames("Host(`nexus/ns1.example.com`)")
        assert hostnames == []


class TestTraefikFilterMethods:
    """Tests for Traefik filter methods."""

    def test_matches_filter_with_wildcard(self) -> None:
        """Test _matches_filter with wildcard pattern."""
        provider = TraefikProxyProvider()
        assert provider._matches_filter("app-internal@docker", "*-internal*") is True
        assert provider._matches_filter("app-public@docker", "*-internal*") is False

    def test_matches_filter_empty_matches_all(self) -> None:
        """Test _matches_filter with empty pattern matches all."""
        provider = TraefikProxyProvider()
        assert provider._matches_filter("anything@docker", "") is True

    def test_has_middleware_returns_true_when_present(self) -> None:
        """Test _has_middleware returns True when middleware is present."""
        provider = TraefikProxyProvider()
        router = {"middlewares": ["auth@docker", "ratelimit@docker"]}
        assert provider._has_middleware(router, "auth") is True

    def test_has_middleware_returns_false_when_absent(self) -> None:
        """Test _has_middleware returns False when middleware is absent."""
        provider = TraefikProxyProvider()
        router = {"middlewares": ["ratelimit@docker"]}
        assert provider._has_middleware(router, "auth") is False

    def test_has_middleware_with_empty_filter(self) -> None:
        """Test _has_middleware with empty filter always returns True."""
        provider = TraefikProxyProvider()
        router: dict = {}
        assert provider._has_middleware(router, "") is True


class TestTraefikJSONErrorHandling:
    """Tests for Traefik JSON error handling."""

    def test_get_routes_handles_invalid_json(self) -> None:
        """Test get_routes raises exception on malformed JSON response."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.side_effect = json.JSONDecodeError("Invalid", "", 0)
            mock_get.return_value = mock_response

            with pytest.raises(json.JSONDecodeError):
                provider.get_routes(instance)

    def test_get_routes_rejects_non_list_response(self) -> None:
        """A malformed response must not masquerade as a successful empty source."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        # Test with dict instead of list
        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"routers": []}  # Dict, not list
            mock_get.return_value = mock_response

            with pytest.raises(RouteSourceReadError, match="expected a list"):
                provider.get_routes(instance)

    def test_get_routes_skips_non_dict_routers(self) -> None:
        """Test get_routes continues processing when some router entries are invalid."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        mock_routers = [
            {"name": "app1@docker", "rule": "Host(`app1.example.com`)"},
            "not_a_dict",  # Invalid: string instead of dict
            123,  # Invalid: integer instead of dict
            None,  # Invalid: None instead of dict
            {"name": "app2@docker", "rule": "Host(`app2.example.com`)"},
        ]

        with patch("requests.Session.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            mock_get.return_value = mock_response

            routes = provider.get_routes(instance)

            # Should only get 2 valid routes, skipping the 3 invalid entries
            assert len(routes) == 2
            hostnames = {r.hostname for r in routes}
            assert hostnames == {"app1.example.com", "app2.example.com"}


class TestTraefikRetryBehavior:
    """Tests for Traefik retry behavior on transient failures."""

    def test_get_routes_retries_on_transient_failure(self) -> None:
        """Test that get_routes retries on transient failure and succeeds."""
        provider = TraefikProxyProvider()
        instance = ProxyInstance(name="test", url="http://traefik:8080", target_ip="10.0.0.1")

        call_count = 0
        mock_routers = [{"name": "app@docker", "rule": "Host(`app.example.com`)"}]

        def mock_get_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("Connection refused")
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = mock_routers
            return mock_response

        with patch("requests.Session.get", side_effect=mock_get_side_effect):
            with patch("external_dns.cli.time.sleep"):  # Skip sleep delays
                routes = provider.get_routes(instance)

        assert len(routes) == 1
        assert routes[0].hostname == "app.example.com"
        assert call_count == 2  # First failed, second succeeded
