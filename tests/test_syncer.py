"""Unit tests for ExternalDNSSyncer reconciliation logic.

Tests the core sync algorithm that determines what DNS records to add/update/delete,
ensuring correctness of the reconciliation logic.
"""

import re
from pathlib import Path
from typing import Dict, List, Set

from external_dns.cli import (
    DNSProvider,
    DNSProviderReadError,
    DNSRecord,
    DNSZone,
    ExternalDNSSyncer,
    ProxyInstance,
    ProxyRoute,
    ReverseProxyProvider,
    RuntimeSettings,
    StateStore,
    _apply_runtime_config_to_syncer,
)

# =============================================================================
# Mock DNS Provider
# =============================================================================


class MockDNSProvider(DNSProvider):
    """Mock DNS provider with in-memory record storage and call tracking."""

    def __init__(
        self,
        initial_records: List[DNSRecord] | None = None,
        *,
        name: str = "MockDNS",
    ):
        self._records: Dict[str, str] = {}
        self._name = name
        self._url = f"mock://{name}"
        self.add_calls: List[tuple[str, str]] = []
        self.delete_calls: List[tuple[str, str]] = []
        self.update_calls: List[tuple[str, str, str]] = []

        if initial_records:
            for record in initial_records:
                self._records[record.domain] = record.answer

    @property
    def name(self) -> str:
        return self._name

    def test_connection(self) -> bool:
        return True

    def get_records(self) -> List[DNSRecord]:
        return [DNSRecord(domain=d, answer=a) for d, a in self._records.items()]

    def add_record(self, domain: str, answer: str) -> bool:
        self.add_calls.append((domain, answer))
        self._records[domain] = answer
        return True

    def delete_record(self, domain: str, answer: str) -> bool:
        self.delete_calls.append((domain, answer))
        if domain in self._records and self._records[domain] == answer:
            del self._records[domain]
            return True
        return False

    def update_record(self, domain: str, old_answer: str, new_answer: str) -> bool:
        self.update_calls.append((domain, old_answer, new_answer))
        if domain in self._records and self._records[domain] == old_answer:
            self._records[domain] = new_answer
            return True
        return False


class MockGokuProvider(MockDNSProvider):
    """Mock Goku provider that stores alias -> destination records."""

    applies_domain_exclusions = False
    conflicts_fail_closed = True
    record_kind = "GoLink alias"
    record_collection = "GoLink aliases"
    key_name = "alias"
    value_name = "destination"

    def __init__(self, initial_records: List[DNSRecord] | None = None):
        super().__init__(initial_records=initial_records, name="MockGoku")

    def desired_static_record(self, domain: str, answer: str) -> DNSRecord | None:
        return None

    def desired_source_record(self, hostname: str, source: Dict[str, object]) -> DNSRecord | None:
        if source.get("golink_enabled") is False:
            return None
        alias = str(source.get("golink_alias") or "").strip()
        destination = str(source.get("golink_destination") or "").strip()
        if not alias or not destination:
            return None
        return DNSRecord(alias, destination)

    def desired_source_records(self, hostname: str, source: Dict[str, object]) -> List[DNSRecord]:
        if source.get("golink_enabled") is False:
            return []
        destination = str(source.get("golink_destination") or "").strip()
        if not destination:
            return []
        raw_aliases = source.get("golink_aliases")
        aliases = raw_aliases if isinstance(raw_aliases, list) else [source.get("golink_alias")]
        records: List[DNSRecord] = []
        seen: set[str] = set()
        for alias in aliases:
            normalized = str(alias or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            records.append(DNSRecord(normalized, destination))
        return records

    def choose_conflicting_record(
        self,
        record_name: str,
        candidates: List[tuple[str, str, str]],
    ) -> tuple[str, str, str] | None:
        exact = [
            candidate
            for candidate in candidates
            if candidate[2].split(".", 1)[0].lower() == record_name.lower()
        ]
        if len(exact) == 1:
            return exact[0]
        return None

    def conflict_hint(self) -> str:
        return "set sources[].golink_alias_template or an explicit alias label"


# =============================================================================
# Mock Reverse Proxy Provider
# =============================================================================


class MockProxyProvider(ReverseProxyProvider):
    """Mock reverse proxy provider with configurable instances and routes."""

    def __init__(
        self,
        instances: List[ProxyInstance],
        routes_by_instance: Dict[str, List[ProxyRoute]],
        failing_instances: Set[str] | None = None,
    ):
        self._instances = instances
        self._routes_by_instance = routes_by_instance
        self._failing_instances = failing_instances or set()

    @property
    def name(self) -> str:
        return "MockProxy"

    def get_instances(self) -> List[ProxyInstance]:
        return self._instances

    def get_routes(self, instance: ProxyInstance) -> List[ProxyRoute]:
        import requests

        if instance.name in self._failing_instances:
            raise requests.exceptions.ConnectionError(
                f"HTTPConnectionPool(host='{instance.name}', port=8080): Max retries exceeded "
                "(Caused by NewConnectionError: Connection refused)"
            )
        return self._routes_by_instance.get(instance.name, [])


# =============================================================================
# Test Helpers
# =============================================================================


def create_test_syncer(
    tmp_path: Path,
    dns_records: List[DNSRecord] | None = None,
    proxy_instances: List[ProxyInstance] | None = None,
    proxy_routes: Dict[str, List[ProxyRoute]] | None = None,
    static_rewrites: Dict[str, str] | None = None,
    exclude_patterns: List[re.Pattern] | None = None,
    failing_instances: Set[str] | None = None,
    takeover_existing_records: bool = False,
) -> tuple[ExternalDNSSyncer, MockDNSProvider, MockProxyProvider]:
    """Create a test syncer with mocked providers.

    Returns tuple of (syncer, dns_provider, proxy_provider) for verification.
    """
    dns_provider = MockDNSProvider(initial_records=dns_records)
    proxy_provider = MockProxyProvider(
        instances=proxy_instances or [],
        routes_by_instance=proxy_routes or {},
        failing_instances=failing_instances,
    )
    state_store = StateStore(str(tmp_path / "state.json"))

    syncer = ExternalDNSSyncer(
        dns_provider=dns_provider,
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites=static_rewrites or {},
        exclude_patterns=exclude_patterns or [],
        takeover_existing_records=takeover_existing_records,
    )

    return syncer, dns_provider, proxy_provider


def create_test_syncer_with_dns_providers(
    tmp_path: Path,
    dns_providers: List[MockDNSProvider],
    proxy_instances: List[ProxyInstance] | None = None,
    proxy_routes: Dict[str, List[ProxyRoute]] | None = None,
    static_rewrites: Dict[str, str] | None = None,
    exclude_patterns: List[re.Pattern] | None = None,
    failing_instances: Set[str] | None = None,
    takeover_existing_records: bool = False,
) -> tuple[ExternalDNSSyncer, MockProxyProvider]:
    """Create a syncer with multiple DNS providers."""
    proxy_provider = MockProxyProvider(
        instances=proxy_instances or [],
        routes_by_instance=proxy_routes or {},
        failing_instances=failing_instances,
    )
    syncer = ExternalDNSSyncer(
        dns_providers=dns_providers,
        proxy_provider=proxy_provider,
        state_store=StateStore(str(tmp_path / "state.json")),
        static_rewrites=static_rewrites or {},
        exclude_patterns=exclude_patterns or [],
        takeover_existing_records=takeover_existing_records,
    )
    return syncer, proxy_provider


def make_instance(name: str, target_ip: str = "10.0.0.1") -> ProxyInstance:
    """Create a ProxyInstance for testing."""
    return ProxyInstance(name=name, url=f"http://{name}:8080", target_ip=target_ip)


def make_route(
    hostname: str,
    target_ip: str = "10.0.0.1",
    zone: DNSZone = DNSZone.INTERNAL,
    source_name: str = "router1",
    publish_external: bool = False,
    golink_alias: str = "",
    golink_aliases: list[str] | None = None,
    golink_destination: str = "",
    golink_enabled: bool = True,
) -> ProxyRoute:
    """Create a ProxyRoute for testing."""
    return ProxyRoute(
        hostname=hostname,
        source_name=source_name,
        target_ip=target_ip,
        zone=zone,
        router_name=source_name,
        publish_external=publish_external,
        golink_alias=golink_alias,
        golink_aliases=golink_aliases or [],
        golink_destination=golink_destination,
        golink_enabled=golink_enabled,
    )


# =============================================================================
# Basic CRUD Operations
# =============================================================================


def test_sync_adds_new_record_when_route_discovered(tmp_path: Path) -> None:
    """New route discovered should result in DNS record creation."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.1"


def test_render_plan_does_not_write_records(tmp_path: Path, caplog) -> None:
    """Render plan previews desired DNS changes without mutating DNS or state."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}
    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    caplog.set_level("INFO")

    assert syncer.render_plan_once() is True

    assert dns.add_calls == []
    assert dns.delete_calls == []
    assert not (tmp_path / "state.json").exists()
    assert "CREATE   app.example.com -> 10.0.0.1" in caplog.text


def test_sync_removes_record_when_route_removed(tmp_path: Path) -> None:
    """Route disappearing should result in DNS record deletion."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}  # No routes now

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    # First sync to establish state with the domain (including managed_records)
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "app.example.com" not in records


def test_sync_updates_record_when_target_ip_changes(tmp_path: Path) -> None:
    """Same domain with new IP should result in record update (if managed)."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core", target_ip="10.0.0.2")]
    routes = {"core": [make_route("app.example.com", "10.0.0.2")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    # Pre-populate state to indicate record is managed
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )

    syncer.sync_once()

    # The syncer deletes old and adds new (not using update_record directly)
    assert ("app.example.com", "10.0.0.1") in dns.delete_calls
    assert ("app.example.com", "10.0.0.2") in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.2"


# =============================================================================
# Multi-Instance Scenarios
# =============================================================================


def test_sync_uses_first_instance_ip_for_conflicting_domains(tmp_path: Path) -> None:
    """Domain on multiple instances with different IPs should use first instance's IP."""
    instances = [make_instance("core", "10.0.0.1"), make_instance("edge", "10.0.0.2")]
    routes = {
        "core": [make_route("app.example.com", "10.0.0.1")],
        "edge": [make_route("app.example.com", "10.0.0.2")],
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    records = {r.domain: r.answer for r in dns.get_records()}
    # First instance (core) should win
    assert records.get("app.example.com") == "10.0.0.1"


def test_sync_preserves_record_when_one_instance_fails(tmp_path: Path, caplog) -> None:
    """Instance unreachable should preserve records from that instance (not delete)."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [ProxyInstance(name="core", url="http://127.0.0.1:18080", target_ip="10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    # Pre-populate state as if a previous sync succeeded
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {
                    "last_success": 1000,
                    "last_error": "",
                    "url": "http://127.0.0.1:18080",
                }
            },
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 1000}}}
            },
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        failing_instances={"core"},  # Instance fails
    )

    syncer.sync_once()

    # Record should NOT be deleted because instance is failing
    assert ("app.example.com", "10.0.0.1") not in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.1"
    source_failures = [
        record.message for record in caplog.records if "Source 'core' unavailable" in record.message
    ]
    assert source_failures == [
        "Source 'core' unavailable (Traefik at http://127.0.0.1:18080): "
        "connection refused; check the local listener or tunnel; keeping last-known routes"
    ]


def test_sync_removes_orphaned_records_when_instance_removed(tmp_path: Path) -> None:
    """Instance removed from config should clean up its managed DNS records."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    # Only one instance now, but state has record from old instance
    instances = [make_instance("edge", "10.0.0.2")]
    routes: Dict[str, List[ProxyRoute]] = {"edge": []}

    # Pre-populate state with domain owned by removed instance "core" (including managed_records)
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {"last_success": 1000, "last_error": "", "url": "http://core:8080"},
            },
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 1000}}},
            },
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    # Record should be deleted because instance "core" is no longer configured
    assert ("app.example.com", "10.0.0.1") in dns.delete_calls


# =============================================================================
# Domain Filtering
# =============================================================================


def test_sync_excludes_domains_matching_exact_pattern(tmp_path: Path) -> None:
    """Exact exclusion pattern should prevent domain from syncing."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("auth.example.com", "10.0.0.1")]}
    patterns = [re.compile(r"^auth\.example\.com$")]

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        exclude_patterns=patterns,
    )

    syncer.sync_once()

    # Should NOT add the excluded domain
    assert len(dns.add_calls) == 0
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "auth.example.com" not in records


def test_sync_excludes_domains_matching_wildcard_pattern(tmp_path: Path) -> None:
    """Wildcard exclusion should prevent matching domains from syncing."""
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("app.internal.example.com", "10.0.0.1"),
            make_route("app.public.example.com", "10.0.0.1"),
        ]
    }
    # Wildcard pattern converted to regex: *.internal.*
    patterns = [re.compile(r".*\.internal\..*")]

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        exclude_patterns=patterns,
    )

    syncer.sync_once()

    # Only public domain should be added
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "app.internal.example.com" not in records
    assert "app.public.example.com" in records


def test_sync_excludes_domains_matching_regex_pattern(tmp_path: Path) -> None:
    """Regex exclusion should prevent matching domains from syncing."""
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("dev-42.example.com", "10.0.0.1"),
            make_route("prod.example.com", "10.0.0.1"),
        ]
    }
    # Regex pattern to exclude dev-{number}.example.com
    patterns = [re.compile(r"^dev-\d+\.example\.com$")]

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        exclude_patterns=patterns,
    )

    syncer.sync_once()

    # Only prod domain should be added
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "dev-42.example.com" not in records
    assert "prod.example.com" in records


def test_sync_removes_existing_excluded_domain_records(tmp_path: Path) -> None:
    """Newly excluded domain should have its managed DNS record deleted."""
    initial_records = [DNSRecord("auth.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    patterns = [re.compile(r"^auth\.example\.com$")]

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        exclude_patterns=patterns,
    )

    # Pre-populate state to indicate record is managed
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records": {"auth.example.com": ["10.0.0.1"]},
        }
    )

    syncer.sync_once()

    # Excluded domain should be removed from DNS (only if managed)
    assert ("auth.example.com", "10.0.0.1") in dns.delete_calls


# =============================================================================
# Zone Handling
# =============================================================================


def test_sync_skips_external_zone_domains(tmp_path: Path) -> None:
    """External zone routes should not be added to DNS."""
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("external.example.com", "10.0.0.1", zone=DNSZone.EXTERNAL),
        ]
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    # External zone domain should NOT be added
    assert len(dns.add_calls) == 0
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "external.example.com" not in records


def test_external_route_uses_public_target_ip(tmp_path: Path) -> None:
    """Explicitly published external routes should be added with their public IP."""
    instances = [make_instance("edge", "10.0.0.1")]
    routes = {
        "edge": [
            make_route(
                "app.example.com",
                "203.0.113.40",
                zone=DNSZone.EXTERNAL,
                source_name="edge",
                publish_external=True,
            ),
        ]
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    assert ("app.example.com", "203.0.113.40") in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "203.0.113.40"


def test_sync_source_order_applies_to_public_external_conflicts(tmp_path: Path) -> None:
    """Configured source order should still choose the DNS answer for public routes."""
    instances = [make_instance("edge", "10.0.0.1"), make_instance("core", "10.0.0.2")]
    routes = {
        "edge": [
            make_route(
                "app.example.com",
                "203.0.113.50",
                zone=DNSZone.EXTERNAL,
                source_name="edge",
                publish_external=True,
            ),
        ],
        "core": [make_route("app.example.com", "10.0.0.2", source_name="core")],
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "203.0.113.50"


def test_sync_only_syncs_internal_zone_domains(tmp_path: Path) -> None:
    """Mix of zones should only sync internal zones."""
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("internal.example.com", "10.0.0.1", zone=DNSZone.INTERNAL),
            make_route("external.example.com", "10.0.0.1", zone=DNSZone.EXTERNAL),
        ]
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    records = {r.domain: r.answer for r in dns.get_records()}
    assert "internal.example.com" in records
    assert "external.example.com" not in records


# =============================================================================
# Static Rewrites
# =============================================================================


def test_sync_adds_missing_static_rewrite(tmp_path: Path) -> None:
    """Static rewrite not in DNS should be added."""
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    static_rewrites = {"static.example.com": "10.0.0.99"}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        static_rewrites=static_rewrites,
    )

    syncer.sync_once()

    assert ("static.example.com", "10.0.0.99") in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("static.example.com") == "10.0.0.99"


def test_sync_updates_static_rewrite_with_wrong_ip(tmp_path: Path) -> None:
    """Static rewrite with different IP should be updated (if managed)."""
    initial_records = [DNSRecord("static.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    static_rewrites = {"static.example.com": "10.0.0.99"}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        static_rewrites=static_rewrites,
    )

    # Pre-populate state to indicate record is managed
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records": {"static.example.com": ["10.0.0.1"]},
        }
    )

    syncer.sync_once()

    # Static rewrite should be updated
    assert ("static.example.com", "10.0.0.1", "10.0.0.99") in dns.update_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("static.example.com") == "10.0.0.99"


def test_sync_preserves_static_rewrite_on_route_removal(tmp_path: Path) -> None:
    """Static rewrite domain removed from routes should NOT be deleted from DNS."""
    initial_records = [DNSRecord("static.example.com", "10.0.0.99")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}  # No routes
    static_rewrites = {"static.example.com": "10.0.0.99"}

    # Pre-populate state with domain that had a route source
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {"last_success": 1000, "last_error": "", "url": "http://core:8080"}
            },
            "domains": {
                "static.example.com": {
                    "sources": {"core": {"answer": "10.0.0.99", "last_seen": 1000}}
                }
            },
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        static_rewrites=static_rewrites,
    )

    syncer.sync_once()

    # Static rewrite should NOT be deleted
    assert ("static.example.com", "10.0.0.99") not in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("static.example.com") == "10.0.0.99"


# =============================================================================
# Edge Cases
# =============================================================================


def test_sync_handles_empty_routes(tmp_path: Path) -> None:
    """No routes discovered should result in no records added."""
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    assert len(dns.add_calls) == 0
    assert len(dns.get_records()) == 0


def test_sync_handles_duplicate_dns_records(tmp_path: Path) -> None:
    """Multiple managed DNS records for same domain should be consolidated to one."""
    # Create provider with duplicates by directly manipulating internal state
    dns_provider = MockDNSProvider()
    dns_provider._records["app.example.com"] = "10.0.0.1"
    # Manually add duplicate by overriding get_records

    def get_records_with_duplicates() -> List[DNSRecord]:
        return [
            DNSRecord("app.example.com", "10.0.0.1"),
            DNSRecord("app.example.com", "10.0.0.2"),
        ]

    dns_provider.get_records = get_records_with_duplicates  # type: ignore[method-assign]

    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.3")]}
    proxy_provider = MockProxyProvider(instances=instances, routes_by_instance=routes)
    state_store = StateStore(str(tmp_path / "state.json"))

    # Pre-populate state to indicate records are managed
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records": {"app.example.com": ["10.0.0.1", "10.0.0.2"]},
        }
    )

    syncer = ExternalDNSSyncer(
        dns_provider=dns_provider,
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    # Both managed duplicates should be deleted and correct record added
    assert ("app.example.com", "10.0.0.1") in dns_provider.delete_calls
    assert ("app.example.com", "10.0.0.2") in dns_provider.delete_calls
    assert ("app.example.com", "10.0.0.3") in dns_provider.add_calls


def test_sync_idempotent_on_repeated_calls(tmp_path: Path) -> None:
    """Same state synced twice should result in no changes second time."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    # First sync
    syncer.sync_once()
    first_add_count = len(dns.add_calls)
    first_delete_count = len(dns.delete_calls)

    # Second sync (should be idempotent)
    syncer.sync_once()

    # No new add/delete calls
    assert len(dns.add_calls) == first_add_count
    assert len(dns.delete_calls) == first_delete_count


def test_sync_handles_no_instances(tmp_path: Path) -> None:
    """No proxy instances configured should handle gracefully."""
    instances: List[ProxyInstance] = []
    routes: Dict[str, List[ProxyRoute]] = {}

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    assert len(dns.add_calls) == 0
    assert len(dns.delete_calls) == 0


def test_sync_handles_multiple_domains_from_single_instance(tmp_path: Path) -> None:
    """Multiple domains from one instance should all be synced."""
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("app1.example.com", "10.0.0.1"),
            make_route("app2.example.com", "10.0.0.1"),
            make_route("app3.example.com", "10.0.0.1"),
        ]
    }

    syncer, dns, _ = create_test_syncer(tmp_path, proxy_instances=instances, proxy_routes=routes)

    syncer.sync_once()

    records = {r.domain: r.answer for r in dns.get_records()}
    assert len(records) == 3
    assert records.get("app1.example.com") == "10.0.0.1"
    assert records.get("app2.example.com") == "10.0.0.1"
    assert records.get("app3.example.com") == "10.0.0.1"


# =============================================================================
# Multi-DNS Provider Scenarios
# =============================================================================


def test_sync_reconciles_each_dns_provider(tmp_path: Path) -> None:
    """Each DNS provider reconciles against its own current records."""
    primary = MockDNSProvider(
        [DNSRecord("app.example.com", "10.0.0.1")],
        name="primary",
    )
    secondary = MockDNSProvider(name="secondary")
    instances = [make_instance("core", "10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [primary, secondary],
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    assert primary.add_calls == []
    assert ("app.example.com", "10.0.0.1") in secondary.add_calls
    assert {r.domain: r.answer for r in primary.get_records()}["app.example.com"] == "10.0.0.1"
    assert {r.domain: r.answer for r in secondary.get_records()}["app.example.com"] == "10.0.0.1"


def test_sync_updates_each_dns_provider_from_its_own_records(tmp_path: Path) -> None:
    """Managed updates are decided from each provider's existing answer."""
    primary = MockDNSProvider(
        [DNSRecord("app.example.com", "10.0.0.9")],
        name="primary",
    )
    secondary = MockDNSProvider(
        [DNSRecord("app.example.com", "10.0.0.8")],
        name="secondary",
    )
    instances = [make_instance("core", "10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records_by_provider": {
                "primary:mock://primary": {"app.example.com": ["10.0.0.9"]},
                "secondary:mock://secondary": {"app.example.com": ["10.0.0.8"]},
            },
        }
    )
    proxy_provider = MockProxyProvider(instances=instances, routes_by_instance=routes)
    syncer = ExternalDNSSyncer(
        dns_providers=[primary, secondary],
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.9") in primary.delete_calls
    assert ("app.example.com", "10.0.0.8") in secondary.delete_calls
    assert ("app.example.com", "10.0.0.1") in primary.add_calls
    assert ("app.example.com", "10.0.0.1") in secondary.add_calls


def test_sync_static_rewrites_apply_to_each_dns_provider(tmp_path: Path) -> None:
    """Static rewrites are ensured independently for every DNS provider."""
    primary = MockDNSProvider(
        [DNSRecord("static.example.com", "10.0.0.99")],
        name="primary",
    )
    secondary = MockDNSProvider(name="secondary")

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [primary, secondary],
        proxy_instances=[make_instance("core")],
        proxy_routes={"core": []},
        static_rewrites={"static.example.com": "10.0.0.99"},
    )

    syncer.sync_once()

    assert primary.add_calls == []
    assert ("static.example.com", "10.0.0.99") in secondary.add_calls


def test_sync_provider_failure_does_not_block_other_dns_providers(tmp_path: Path) -> None:
    """A failed DNS provider write does not stop later providers from reconciling."""
    failing = MockDNSProvider(name="failing")
    healthy = MockDNSProvider(name="healthy")

    def failing_add(domain: str, answer: str) -> bool:
        failing.add_calls.append((domain, answer))
        return False

    failing.add_record = failing_add  # type: ignore[method-assign]

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [failing, healthy],
        proxy_instances=[make_instance("core", "10.0.0.1")],
        proxy_routes={"core": [make_route("app.example.com", "10.0.0.1")]},
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in failing.add_calls
    assert ("app.example.com", "10.0.0.1") in healthy.add_calls


def test_sync_skips_provider_when_records_cannot_be_read(tmp_path: Path, caplog) -> None:
    """A read failure does not make a provider look empty and writable."""
    unreadable = MockDNSProvider(name="unreadable")
    healthy = MockDNSProvider(name="healthy")

    def failing_get_records() -> List[DNSRecord]:
        raise DNSProviderReadError("read failed")

    unreadable.get_records = failing_get_records  # type: ignore[method-assign]

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [unreadable, healthy],
        proxy_instances=[make_instance("core", "10.0.0.1")],
        proxy_routes={"core": [make_route("app.example.com", "10.0.0.1")]},
    )

    syncer.sync_once()

    assert unreadable.add_calls == []
    assert ("app.example.com", "10.0.0.1") in healthy.add_calls
    target_failures = [
        record.message
        for record in caplog.records
        if record.message.startswith("[unreadable] DNS records unavailable")
    ]
    assert target_failures == [
        "[unreadable] DNS records unavailable from unreadable at mock://unreadable: "
        "read failed; record reconciliation skipped"
    ]


# =============================================================================
# Graceful Degradation Tests
# =============================================================================


def test_sync_continues_when_dns_provider_unavailable(tmp_path: Path) -> None:
    """DNS provider errors should be logged but not crash sync."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    # Make DNS provider return errors
    def failing_add(domain: str, answer: str) -> bool:
        dns.add_calls.append((domain, answer))
        return False  # Simulate failure

    dns.add_record = failing_add  # type: ignore[method-assign]

    # sync_once should complete without raising
    syncer.sync_once()

    # Verify the attempt was made
    assert ("app.example.com", "10.0.0.1") in dns.add_calls

    # State file should still be saved
    state = syncer.state_store.load()
    assert "instances" in state
    assert "domains" in state


def test_sync_handles_all_instances_failing(tmp_path: Path) -> None:
    """All proxy instances failing should preserve state and not crash."""
    initial_records = [
        DNSRecord("app1.example.com", "10.0.0.1"),
        DNSRecord("app2.example.com", "10.0.0.2"),
    ]
    instances = [make_instance("core", "10.0.0.1"), make_instance("edge", "10.0.0.2")]
    routes = {
        "core": [make_route("app1.example.com", "10.0.0.1")],
        "edge": [make_route("app2.example.com", "10.0.0.2")],
    }

    # Pre-populate state
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {"last_success": 1000, "last_error": "", "url": "http://core:8080"},
                "edge": {"last_success": 1000, "last_error": "", "url": "http://edge:8080"},
            },
            "domains": {
                "app1.example.com": {
                    "sources": {"core": {"answer": "10.0.0.1", "last_seen": 1000}}
                },
                "app2.example.com": {
                    "sources": {"edge": {"answer": "10.0.0.2", "last_seen": 1000}}
                },
            },
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        failing_instances={"core", "edge"},  # All instances fail
    )

    # sync_once should complete without raising
    syncer.sync_once()

    # Records should NOT be deleted (instances are failing)
    assert ("app1.example.com", "10.0.0.1") not in dns.delete_calls
    assert ("app2.example.com", "10.0.0.2") not in dns.delete_calls

    # State should be preserved with error info
    state = syncer.state_store.load()
    assert "core" in state["instances"]
    assert "edge" in state["instances"]
    assert state["instances"]["core"]["last_error"] != ""
    assert state["instances"]["edge"]["last_error"] != ""


def test_sync_recovers_after_transient_failure(tmp_path: Path) -> None:
    """Instance that fails then succeeds should sync correctly on recovery."""
    instances = [make_instance("core", "10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    # Pre-populate state with previous error
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {
                    "last_success": 500,
                    "last_error": "Connection refused",
                    "url": "http://core:8080",
                }
            },
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 500}}}
            },
        }
    )

    # Create syncer with working instance (no longer failing)
    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        failing_instances=set(),  # No failures now
    )

    syncer.sync_once()

    # Instance should recover - last_error should be cleared
    state = syncer.state_store.load()
    assert state["instances"]["core"]["last_error"] == ""
    assert state["instances"]["core"]["last_success"] > 500

    # Record should still exist (either kept or re-added if needed)
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.1"


def test_sync_state_not_corrupted_on_partial_failure(tmp_path: Path) -> None:
    """Partial failures should not corrupt state file."""
    instances = [make_instance("core", "10.0.0.1"), make_instance("edge", "10.0.0.2")]
    routes = {
        "core": [make_route("app1.example.com", "10.0.0.1")],
        "edge": [make_route("app2.example.com", "10.0.0.2")],
    }

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
        failing_instances={"edge"},  # Only edge fails
    )

    syncer.sync_once()

    # State should be valid JSON and contain expected structure
    state = syncer.state_store.load()
    assert state["version"] == 1
    assert "instances" in state
    assert "domains" in state

    # core instance should have succeeded
    assert state["instances"]["core"]["last_error"] == ""
    assert state["instances"]["core"]["last_success"] > 0

    # edge instance should have failed
    assert "edge" in state["instances"]
    assert state["instances"]["edge"]["last_error"] != ""

    # app1 domain from core should be in state
    assert "app1.example.com" in state["domains"]


# =============================================================================
# Pre-existing Record Protection Tests
# =============================================================================


def test_sync_preserves_preexisting_records_on_domain_removal(tmp_path: Path) -> None:
    """Pre-existing (unmanaged) records should NOT be deleted when domain is removed from proxy."""
    # DNS has a pre-existing record that external-dns didn't create
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}  # No routes

    # State has the domain tracked but NOT in managed_records (pre-existing)
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            # Note: NO managed_records entry - this record was pre-existing
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    # Pre-existing record should NOT be deleted
    assert ("app.example.com", "10.0.0.1") not in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.1"


def test_sync_preserves_preexisting_records_when_proxy_wants_different_ip(tmp_path: Path) -> None:
    """Pre-existing records should NOT be overwritten when proxy advertises different IP."""
    # DNS has a pre-existing record
    initial_records = [DNSRecord("app.example.com", "192.168.1.100")]
    instances = [make_instance("core", target_ip="10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    # Pre-existing record should NOT be deleted or modified
    assert ("app.example.com", "192.168.1.100") not in dns.delete_calls
    # New record should NOT be added (conflict with pre-existing)
    assert ("app.example.com", "10.0.0.1") not in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "192.168.1.100"


def test_sync_takes_over_preexisting_records_when_enabled(tmp_path: Path) -> None:
    """Configured takeover should replace unmanaged records with desired proxy answers."""
    initial_records = [DNSRecord("app.example.com", "192.168.1.100")]
    instances = [make_instance("core", target_ip="10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        takeover_existing_records=True,
    )

    syncer.sync_once()

    assert ("app.example.com", "192.168.1.100") in dns.delete_calls
    assert ("app.example.com", "10.0.0.1") in dns.add_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("app.example.com") == "10.0.0.1"


def test_sync_preserves_preexisting_excluded_records(tmp_path: Path) -> None:
    """Pre-existing records matching exclusion patterns should NOT be deleted."""
    initial_records = [DNSRecord("auth.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    patterns = [re.compile(r"^auth\.example\.com$")]

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        exclude_patterns=patterns,
    )

    # No managed_records - record is pre-existing
    syncer.sync_once()

    # Pre-existing record should NOT be deleted
    assert ("auth.example.com", "10.0.0.1") not in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("auth.example.com") == "10.0.0.1"


def test_sync_preserves_preexisting_static_rewrite_with_wrong_ip(tmp_path: Path) -> None:
    """Pre-existing records should NOT be updated even for static rewrites."""
    initial_records = [DNSRecord("static.example.com", "192.168.1.100")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    static_rewrites = {"static.example.com": "10.0.0.99"}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
        static_rewrites=static_rewrites,
    )

    # No managed_records - record is pre-existing
    syncer.sync_once()

    # Pre-existing record should NOT be updated
    assert len(dns.update_calls) == 0
    records = {r.domain: r.answer for r in dns.get_records()}
    assert records.get("static.example.com") == "192.168.1.100"


def test_sync_adopts_preexisting_record_with_matching_answer(tmp_path: Path) -> None:
    """Pre-existing record with matching answer should be adopted as managed."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core", target_ip="10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    # No changes to DNS
    assert len(dns.add_calls) == 0
    assert len(dns.delete_calls) == 0

    # Record should now be tracked as managed
    state = syncer.state_store.load()
    assert "app.example.com" in state.get("managed_records", {})
    assert "10.0.0.1" in state["managed_records"]["app.example.com"]

    # On subsequent sync, if domain is removed, it SHOULD be deleted (now managed)
    syncer2, dns2, _ = create_test_syncer(
        tmp_path,
        dns_records=[DNSRecord("app.example.com", "10.0.0.1")],
        proxy_instances=[make_instance("core", target_ip="10.0.0.1")],
        proxy_routes={"core": []},  # Domain removed
    )

    syncer2.sync_once()

    # Now it should be deleted because it's managed
    assert ("app.example.com", "10.0.0.1") in dns2.delete_calls


def test_sync_managed_records_tracked_across_syncs(tmp_path: Path) -> None:
    """Records created by external-dns should be tracked and deletable."""
    instances = [make_instance("core")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    # First sync - creates record
    syncer.sync_once()
    assert ("app.example.com", "10.0.0.1") in dns.add_calls

    # Verify record is tracked as managed
    state = syncer.state_store.load()
    assert "app.example.com" in state.get("managed_records", {})

    # Second sync with domain removed - should delete
    syncer2, dns2, _ = create_test_syncer(
        tmp_path,
        dns_records=[DNSRecord("app.example.com", "10.0.0.1")],
        proxy_instances=[make_instance("core")],
        proxy_routes={"core": []},  # No routes
    )

    syncer2.sync_once()

    # Should be deleted because it's managed
    assert ("app.example.com", "10.0.0.1") in dns2.delete_calls


def test_sync_managed_records_are_provider_scoped(tmp_path: Path) -> None:
    """One provider's managed record does not authorize deletion in another provider."""
    primary = MockDNSProvider(
        [DNSRecord("app.example.com", "10.0.0.1")],
        name="primary",
    )
    secondary = MockDNSProvider(
        [DNSRecord("app.example.com", "10.0.0.1")],
        name="secondary",
    )
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            "managed_records_by_provider": {
                "primary:mock://primary": {"app.example.com": ["10.0.0.1"]}
            },
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )
    proxy_provider = MockProxyProvider(
        instances=[make_instance("core")],
        routes_by_instance={"core": []},
    )
    syncer = ExternalDNSSyncer(
        dns_providers=[primary, secondary],
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in primary.delete_calls
    assert ("app.example.com", "10.0.0.1") not in secondary.delete_calls
    assert {r.domain: r.answer for r in secondary.get_records()}["app.example.com"] == "10.0.0.1"


def test_sync_reads_legacy_managed_records_without_provider_state(tmp_path: Path) -> None:
    """Legacy managed_records state remains eligible for cleanup before migration."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    StateStore(str(tmp_path / "state.json")).save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.delete_calls


def test_sync_migrates_all_legacy_managed_records_before_reconciliation(
    tmp_path: Path,
) -> None:
    """Legacy state migration is complete before the first provider-scoped mark."""
    initial_records = [
        DNSRecord("app.example.com", "10.0.0.1"),
        DNSRecord("api.example.com", "10.0.0.2"),
    ]
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route("app.example.com", "10.0.0.1"),
            make_route("api.example.com", "10.0.0.3"),
        ]
    }
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}},
                "api.example.com": {"sources": {"core": {"answer": "10.0.0.2", "last_seen": 0}}},
            },
            "managed_records": {
                "app.example.com": ["10.0.0.1"],
                "api.example.com": ["10.0.0.2"],
            },
        }
    )
    dns = MockDNSProvider(initial_records=initial_records)
    proxy_provider = MockProxyProvider(instances=instances, routes_by_instance=routes)
    syncer = ExternalDNSSyncer(
        dns_provider=dns,
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    records = {record.domain: record.answer for record in dns.get_records()}
    assert records["app.example.com"] == "10.0.0.1"
    assert records["api.example.com"] == "10.0.0.3"
    assert ("api.example.com", "10.0.0.2") in dns.delete_calls
    assert ("api.example.com", "10.0.0.3") in dns.add_calls
    state = state_store.load()
    provider_key = syncer._provider_state_key(dns)
    assert state["managed_records_by_provider"][provider_key] == {
        "app.example.com": ["10.0.0.1"],
        "api.example.com": ["10.0.0.3"],
    }


def test_sync_merges_legacy_records_into_partial_provider_state(tmp_path: Path) -> None:
    """Partial provider-scoped state still adopts remaining legacy-managed records."""
    initial_records = [
        DNSRecord("app.example.com", "10.0.0.1"),
        DNSRecord("api.example.com", "10.0.0.2"),
    ]
    instances = [make_instance("core")]
    routes: Dict[str, List[ProxyRoute]] = {"core": []}
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}},
                "api.example.com": {"sources": {"core": {"answer": "10.0.0.2", "last_seen": 0}}},
            },
            "managed_records_by_provider": {
                "MockDNS:mock://MockDNS": {"app.example.com": ["10.0.0.1"]}
            },
            "managed_records": {
                "app.example.com": ["10.0.0.1"],
                "api.example.com": ["10.0.0.2"],
            },
        }
    )
    dns = MockDNSProvider(initial_records=initial_records)
    proxy_provider = MockProxyProvider(instances=instances, routes_by_instance=routes)
    syncer = ExternalDNSSyncer(
        dns_provider=dns,
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.delete_calls
    assert ("api.example.com", "10.0.0.2") in dns.delete_calls


def test_reload_runtime_config_resets_removed_source_cleanup(tmp_path: Path) -> None:
    """Reloaded sources trigger removed-instance cleanup on the immediate sync."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            "managed_records": {"app.example.com": ["10.0.0.1"]},
        }
    )
    dns = MockDNSProvider(initial_records=initial_records)
    syncer = ExternalDNSSyncer(
        dns_provider=dns,
        proxy_provider=MockProxyProvider(
            instances=[make_instance("core")],
            routes_by_instance={"core": [make_route("app.example.com")]},
        ),
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )
    syncer._startup_cleanup_done = True
    reloaded_proxy = MockProxyProvider(instances=[], routes_by_instance={})

    _apply_runtime_config_to_syncer(
        syncer,
        dns_providers=[dns],
        proxy_provider=reloaded_proxy,
        settings=RuntimeSettings(),
        instances=[],
    )
    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.delete_calls


def test_reload_runtime_config_updates_static_rewrites_and_exclusions(tmp_path: Path) -> None:
    """Reloaded static rewrites and exclusions are applied without restart."""
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {
                "blocked.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
            "managed_records": {"blocked.example.com": ["10.0.0.1"]},
        }
    )
    dns = MockDNSProvider([DNSRecord("blocked.example.com", "10.0.0.1")])
    syncer = ExternalDNSSyncer(
        dns_provider=dns,
        proxy_provider=MockProxyProvider(instances=[], routes_by_instance={}),
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )
    syncer._startup_cleanup_done = True
    settings = RuntimeSettings(
        static_rewrites={"static.example.com": ""},
        exclude_domains="blocked.example.com",
    )
    reloaded_instances = [make_instance("core", "10.0.0.55")]

    _apply_runtime_config_to_syncer(
        syncer,
        dns_providers=[dns],
        proxy_provider=MockProxyProvider(
            instances=reloaded_instances, routes_by_instance={"core": []}
        ),
        settings=settings,
        instances=reloaded_instances,
    )
    syncer.sync_once()

    assert syncer.static_rewrites == {"static.example.com": "10.0.0.55"}
    assert ("static.example.com", "10.0.0.55") in dns.add_calls
    assert ("blocked.example.com", "10.0.0.1") in dns.delete_calls


def test_sync_writes_goku_alias_from_route_metadata(tmp_path: Path) -> None:
    """Goku providers receive alias -> FQDN destination records, not DNS A records."""
    dns = MockDNSProvider(name="MockDNS")
    goku = MockGokuProvider()
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route(
                "photos.example.com",
                "10.0.0.1",
                source_name="core",
                golink_alias="immich",
                golink_destination="https://photos.example.com",
            )
        ]
    }

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [dns, goku],
        proxy_instances=instances,
        proxy_routes=routes,
    )
    syncer.sync_once()

    assert ("photos.example.com", "10.0.0.1") in dns.add_calls
    assert ("immich", "https://photos.example.com") in goku.add_calls


def test_sync_writes_multiple_goku_aliases_from_route_metadata(tmp_path: Path) -> None:
    """One discovered route can publish service and hostname GoLink aliases."""
    goku = MockGokuProvider()
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route(
                "stat.example.com",
                source_name="core",
                golink_alias="kromgo",
                golink_aliases=["kromgo", "stat"],
                golink_destination="https://stat.example.com",
            )
        ]
    }

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [goku],
        proxy_instances=instances,
        proxy_routes=routes,
    )
    syncer.sync_once()

    assert ("kromgo", "https://stat.example.com") in goku.add_calls
    assert ("stat", "https://stat.example.com") in goku.add_calls


def test_sync_goku_duplicate_alias_same_destination_is_ok(tmp_path: Path) -> None:
    """Overlapping sources can publish one alias when they agree on destination."""
    goku = MockGokuProvider()
    instances = [make_instance("mothership"), make_instance("nexus")]
    routes = {
        "mothership": [
            make_route(
                "traefik.example.com",
                source_name="mothership",
                golink_alias="traefik",
                golink_destination="https://traefik.example.com",
            )
        ],
        "nexus": [
            make_route(
                "traefik.example.com",
                source_name="nexus",
                golink_alias="traefik",
                golink_destination="https://traefik.example.com",
            )
        ],
    }

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [goku],
        proxy_instances=instances,
        proxy_routes=routes,
    )
    syncer.sync_once()

    assert goku.add_calls == [("traefik", "https://traefik.example.com")]


def test_sync_goku_prefers_hostname_that_matches_alias(tmp_path: Path) -> None:
    """A canonical hostname match resolves a service-alias collision across sources."""
    goku = MockGokuProvider()
    instances = [make_instance("mothership"), make_instance("nexus")]
    routes = {
        "mothership": [
            make_route(
                "stat.example.com",
                source_name="mothership",
                golink_alias="kromgo",
                golink_aliases=["kromgo", "stat"],
                golink_destination="https://stat.example.com",
            )
        ],
        "nexus": [
            make_route(
                "kromgo.example.com",
                source_name="nexus",
                golink_alias="kromgo",
                golink_destination="https://kromgo.example.com",
            )
        ],
    }

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [goku],
        proxy_instances=instances,
        proxy_routes=routes,
    )
    syncer.sync_once()

    assert ("kromgo", "https://kromgo.example.com") in goku.add_calls
    assert ("stat", "https://stat.example.com") in goku.add_calls


def test_sync_goku_duplicate_alias_conflict_is_skipped_and_managed_alias_removed(
    tmp_path: Path, caplog
) -> None:
    """Same alias with different destinations is not chosen silently."""
    goku = MockGokuProvider([DNSRecord("traefik", "https://old.example.com")])
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records_by_provider": {
                "MockGoku:mock://MockGoku": {"traefik": ["https://old.example.com"]}
            },
        }
    )
    instances = [make_instance("mothership"), make_instance("nexus")]
    routes = {
        "mothership": [
            make_route(
                "traefik.mothership.example.com",
                source_name="mothership",
                golink_alias="traefik",
                golink_destination="https://traefik.mothership.example.com",
            )
        ],
        "nexus": [
            make_route(
                "traefik.nexus.example.com",
                source_name="nexus",
                golink_alias="traefik",
                golink_destination="https://traefik.nexus.example.com",
            )
        ],
    }
    proxy = MockProxyProvider(instances=instances, routes_by_instance=routes)
    syncer = ExternalDNSSyncer(
        dns_providers=[goku],
        proxy_provider=proxy,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    assert goku.add_calls == []
    assert ("traefik", "https://old.example.com") in goku.delete_calls
    assert "Ambiguous golink alias 'traefik'" in caplog.text
    assert "golink_alias_template" in caplog.text


def test_sync_removes_owned_record_absent_from_desired_state(tmp_path: Path) -> None:
    """Stale cleanup follows ownership, without provider-specific conflict flags."""
    target = MockDNSProvider([DNSRecord("orphan.example.com", "10.0.0.9")])
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {},
            "domains": {},
            "managed_records_by_provider": {
                "MockDNS:mock://MockDNS": {"orphan.example.com": ["10.0.0.9"]}
            },
        }
    )
    syncer = ExternalDNSSyncer(
        dns_providers=[target],
        proxy_provider=MockProxyProvider(instances=[], routes_by_instance={}),
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    assert target.delete_calls == [("orphan.example.com", "10.0.0.9")]


def test_sync_goku_route_opt_out_does_not_block_dns_record(tmp_path: Path) -> None:
    """GoLink opt-out only affects Goku; DNS records still reconcile."""
    dns = MockDNSProvider(name="MockDNS")
    goku = MockGokuProvider()
    instances = [make_instance("core")]
    routes = {
        "core": [
            make_route(
                "admin.example.com",
                "10.0.0.5",
                source_name="core",
                golink_alias="admin",
                golink_destination="https://admin.example.com",
                golink_enabled=False,
            )
        ]
    }

    syncer, _ = create_test_syncer_with_dns_providers(
        tmp_path,
        [dns, goku],
        proxy_instances=instances,
        proxy_routes=routes,
    )
    syncer.sync_once()

    assert ("admin.example.com", "10.0.0.5") in dns.add_calls
    assert goku.add_calls == []
