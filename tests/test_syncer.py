"""Unit tests for ExternalDNSSyncer reconciliation logic.

Tests the core sync algorithm that determines what DNS records to add/update/delete,
ensuring correctness of the reconciliation logic.
"""

import re
from pathlib import Path
from typing import Dict, List, Set

from external_dns.cli import (
    DNSProvider,
    DNSRecord,
    DNSZone,
    ExternalDNSSyncer,
    ProxyInstance,
    ProxyRoute,
    ReverseProxyProvider,
    StateStore,
)

# =============================================================================
# Mock DNS Provider
# =============================================================================


class MockDNSProvider(DNSProvider):
    """Mock DNS provider with in-memory record storage and call tracking."""

    def __init__(self, initial_records: List[DNSRecord] | None = None):
        self._records: Dict[str, str] = {}
        self.add_calls: List[tuple[str, str]] = []
        self.delete_calls: List[tuple[str, str]] = []
        self.update_calls: List[tuple[str, str, str]] = []

        if initial_records:
            for record in initial_records:
                self._records[record.domain] = record.answer

    @property
    def name(self) -> str:
        return "MockDNS"

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
            raise requests.exceptions.RequestException(f"Instance {instance.name} unreachable")
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
    )

    return syncer, dns_provider, proxy_provider


def make_instance(name: str, target_ip: str = "10.0.0.1") -> ProxyInstance:
    """Create a ProxyInstance for testing."""
    return ProxyInstance(name=name, url=f"http://{name}:8080", target_ip=target_ip)


def make_route(
    hostname: str,
    target_ip: str = "10.0.0.1",
    zone: DNSZone = DNSZone.INTERNAL,
    source_name: str = "router1",
) -> ProxyRoute:
    """Create a ProxyRoute for testing."""
    return ProxyRoute(
        hostname=hostname,
        source_name=source_name,
        target_ip=target_ip,
        zone=zone,
        router_name=source_name,
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

    # First sync to establish state with the domain
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {"core": {"last_success": 0, "last_error": "", "url": "http://core:8080"}},
            "domains": {
                "app.example.com": {"sources": {"core": {"answer": "10.0.0.1", "last_seen": 0}}}
            },
        }
    )

    syncer.sync_once()

    assert ("app.example.com", "10.0.0.1") in dns.delete_calls
    records = {r.domain: r.answer for r in dns.get_records()}
    assert "app.example.com" not in records


def test_sync_updates_record_when_target_ip_changes(tmp_path: Path) -> None:
    """Same domain with new IP should result in record update."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core", target_ip="10.0.0.2")]
    routes = {"core": [make_route("app.example.com", "10.0.0.2")]}

    syncer, dns, _ = create_test_syncer(
        tmp_path,
        dns_records=initial_records,
        proxy_instances=instances,
        proxy_routes=routes,
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


def test_sync_preserves_record_when_one_instance_fails(tmp_path: Path) -> None:
    """Instance unreachable should preserve records from that instance (not delete)."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    instances = [make_instance("core", "10.0.0.1")]
    routes = {"core": [make_route("app.example.com", "10.0.0.1")]}

    # Pre-populate state as if a previous sync succeeded
    state_store = StateStore(str(tmp_path / "state.json"))
    state_store.save(
        {
            "version": 1,
            "instances": {
                "core": {"last_success": 1000, "last_error": "", "url": "http://core:8080"}
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


def test_sync_removes_orphaned_records_when_instance_removed(tmp_path: Path) -> None:
    """Instance removed from config should clean up its DNS records."""
    initial_records = [DNSRecord("app.example.com", "10.0.0.1")]
    # Only one instance now, but state has record from old instance
    instances = [make_instance("edge", "10.0.0.2")]
    routes: Dict[str, List[ProxyRoute]] = {"edge": []}

    # Pre-populate state with domain owned by removed instance "core"
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
    """Newly excluded domain should have its existing DNS record deleted."""
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

    syncer.sync_once()

    # Excluded domain should be removed from DNS
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
    """Static rewrite with different IP should be updated."""
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
    """Multiple DNS records for same domain should be consolidated to one."""
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

    syncer = ExternalDNSSyncer(
        dns_provider=dns_provider,
        proxy_provider=proxy_provider,
        state_store=state_store,
        static_rewrites={},
        exclude_patterns=[],
    )

    syncer.sync_once()

    # Both duplicates should be deleted and correct record added
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
