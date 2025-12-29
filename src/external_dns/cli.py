#!/usr/bin/env python3
"""external-dns - Universal DNS Synchronization

Syncs reverse proxy routes into DNS providers, similar in spirit to Kubernetes
external-dns. Supports multiple DNS providers and reverse proxy implementations.

Supported DNS Providers:
    - adguard: AdGuard Home DNS rewrites
    (more coming soon)

Supported Reverse Proxy Providers:
    - traefik: Traefik HTTP routers
    (more coming soon)

Environment variables:

    Provider Selection:
        DNS_PROVIDER           DNS provider type: "adguard" (default: adguard)
        PROXY_PROVIDER         Reverse proxy type: "traefik" (default: traefik)

    AdGuard DNS Provider:
        ADGUARD_URL            AdGuard Home base URL (default: http://adguard)
        ADGUARD_USERNAME       Admin username (optional)
        ADGUARD_PASSWORD       Admin password (optional)

    Traefik Reverse Proxy:
        TRAEFIK_CONFIG_PATH    Path to YAML config file with Traefik instances
                               (default: /config/traefik-instances.yaml)
                               Example config file:
                                 instances:
                                   - name: "core"
                                     url: "http://traefik:8080"
                                     target_ip: "10.0.0.2"
                                     verify_tls: true
                                     router_filter: "*-internal"
                                   - name: "edge"
                                     url: "https://traefik2:8080"
                                     target_ip: "10.0.0.3"
                                     verify_tls: false
                                     router_filter: ""

        TRAEFIK_INSTANCES      JSON list of instances (legacy, use config file instead).
                               Example:
                               [{"name":"core","url":"http://traefik:8080","target_ip":"10.0.0.2"},
                                {"name":"edge","url":"https://traefik2:8080","target_ip":"10.0.0.3","verify_tls":false}]

        Backwards-compatible single-instance mode (used if config file and TRAEFIK_INSTANCES unset):
            TRAEFIK_URL          Traefik base URL (default: http://traefik:8080)
            TRAEFIK_TARGET_IP    Target IP to use for rewrites (falls back to INTERNAL_IP)
            INTERNAL_IP          Legacy name for the target IP

    Router Filtering:
        router_filter          Per-instance wildcard pattern to filter routers (in YAML config)
                               Only routers matching this pattern will be synced to DNS
                               Examples: "*-internal", "app-*", "*-public-*"
                               Empty string = no filtering (sync all routers)

    Runtime:
        SYNC_MODE              "once" or "watch" (polling loop) (default: watch)
        POLL_INTERVAL_SECONDS  Poll interval in watch mode (default: 60)
        LOG_LEVEL              DEBUG, INFO, WARNING, ERROR (default: INFO)
        STATE_PATH             JSON state file path (default: /data/state.json)

    Static rewrites:
        EXTERNAL_DNS_STATIC_REWRITES  Comma-separated "domain" or "domain=answer" entries.
                                      Static rewrites are ensured to exist, but are NOT
                                      automatically removed if deleted from this env var.

    Domain exclusions:
        EXTERNAL_DNS_EXCLUDE_DOMAINS  Comma-separated patterns for domains to exclude from sync.
                                  Supports three formats:
                                    - Exact domain: "auth.example.com"
                                    - Wildcard (fnmatch-style): "*.internal.*", "dev-*"
                                    - Regex (prefix with ~): "~^staging-\d+\.example\.com$"
                                  Excluded domains are NOT synced to DNS. Existing
                                  records matching exclusions are cleaned up automatically.

    Zone classification:
        EXTERNAL_DNS_DEFAULT_ZONE     Default zone for routers without explicit zone label.
                                      "internal" (default) or "external".
                                      - internal: Create DNS rewrites in local DNS provider
                                      - external: Skip DNS rewrite (forward to upstream DNS)

        EXTERNAL_DNS_ZONE_LABEL       Label name to check for zone classification.
                                      Default: "external-dns.zone"

        Zone detection priority (first match wins):
          1. Router name suffix: "-internal" or "-external"
          2. Custom label value (from EXTERNAL_DNS_ZONE_LABEL)
          3. Default zone (from EXTERNAL_DNS_DEFAULT_ZONE)

        Example: A service can define multiple routers for different zones:
          traefik.http.routers.myapp-internal.rule: Host(`myapp.local.example.com`)
          traefik.http.routers.myapp-external.rule: Host(`myapp.example.com`)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests
import yaml
from requests.auth import HTTPBasicAuth

# =============================================================================
# File Watching Utilities
# =============================================================================


def get_config_file_mtime(config_path: str) -> float:
    """Get modification time of config file, returns 0 if file doesn't exist."""
    try:
        return os.path.getmtime(config_path) if os.path.exists(config_path) else 0.0
    except (OSError, IOError):
        return 0.0


def find_config_files(config_path: str) -> List[str]:
    """Find all .yaml config files in directory or return single file.

    Args:
        config_path: Path to config file or directory

    Returns:
        List of config file paths (excluding .template files)
    """
    path = Path(config_path)

    # If it's a file, return it directly
    if path.is_file():
        return [str(path)]

    # If it's a directory, scan for .yaml files
    if path.is_dir():
        yaml_files = sorted(path.glob("*.yaml"))
        # Exclude .template files
        return [str(f) for f in yaml_files if not f.name.endswith(".template")]

    # Path doesn't exist yet
    return []


def get_config_files_mtimes(config_files: List[str]) -> Dict[str, float]:
    """Get modification times for all config files."""
    return {f: get_config_file_mtime(f) for f in config_files}


# =============================================================================
# Configuration
# =============================================================================

# Provider selection
DNS_PROVIDER = os.getenv("DNS_PROVIDER", "adguard").lower().strip()
PROXY_PROVIDER = os.getenv("PROXY_PROVIDER", "traefik").lower().strip()

# AdGuard configuration
ADGUARD_URL = os.getenv("ADGUARD_URL", "http://adguard")
ADGUARD_USERNAME = os.getenv("ADGUARD_USERNAME", "")
ADGUARD_PASSWORD = os.getenv("ADGUARD_PASSWORD", "")

# Traefik configuration
TRAEFIK_CONFIG_PATH = os.getenv("TRAEFIK_CONFIG_PATH", "/config/traefik-instances.yaml")
TRAEFIK_INSTANCES = os.getenv("TRAEFIK_INSTANCES", "").strip()
TRAEFIK_URL = os.getenv("TRAEFIK_URL", "http://traefik:8080")
TRAEFIK_TARGET_IP = os.getenv("TRAEFIK_TARGET_IP", os.getenv("INTERNAL_IP", ""))

# Runtime configuration
SYNC_MODE = os.getenv("SYNC_MODE", "watch")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
STATE_PATH = os.getenv("STATE_PATH", "/data/state.json")

# Static rewrites and exclusions
EXTERNAL_DNS_STATIC_REWRITES = os.getenv("EXTERNAL_DNS_STATIC_REWRITES", "")
EXTERNAL_DNS_EXCLUDE_DOMAINS = os.getenv("EXTERNAL_DNS_EXCLUDE_DOMAINS", "")

# Zone configuration
EXTERNAL_DNS_DEFAULT_ZONE = os.getenv("EXTERNAL_DNS_DEFAULT_ZONE", "internal").lower().strip()
EXTERNAL_DNS_ZONE_LABEL = os.getenv("EXTERNAL_DNS_ZONE_LABEL", "external-dns.zone")

# =============================================================================
# Logging Setup
# =============================================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# =============================================================================
# Enums
# =============================================================================


class DNSZone(Enum):
    """DNS zone classification for routing.

    INTERNAL: Create local DNS rewrites pointing to internal IPs.
              These domains are resolved by the internal DNS provider.

    EXTERNAL: Skip local DNS rewrite creation. These domains are resolved
              by upstream DNS servers via the DNS provider's normal
              forwarding behavior.
    """

    INTERNAL = "internal"
    EXTERNAL = "external"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class DNSRecord:
    """Represents a DNS record."""

    domain: str
    answer: str


@dataclass(frozen=True)
class ProxyRoute:
    """Represents a route discovered from a reverse proxy."""

    hostname: str
    source_name: str
    target_ip: str
    zone: DNSZone = DNSZone.INTERNAL
    router_name: str = ""


@dataclass(frozen=True)
class ProxyInstance:
    """Configuration for a reverse proxy instance."""

    name: str
    url: str
    target_ip: str
    type: str = "traefik"
    verify_tls: bool = True
    username: str = ""
    password: str = ""
    router_filter: str = ""
    middleware_filter: str = ""


# =============================================================================
# DNS Provider Interface and Implementations
# =============================================================================


class DNSProvider(ABC):
    """Abstract base class for DNS providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name for logging."""
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """Test connection to the DNS provider."""
        pass

    @abstractmethod
    def get_records(self) -> List[DNSRecord]:
        """Get all DNS records managed by this provider."""
        pass

    @abstractmethod
    def add_record(self, domain: str, answer: str) -> bool:
        """Add a DNS record."""
        pass

    @abstractmethod
    def delete_record(self, domain: str, answer: str) -> bool:
        """Delete a DNS record."""
        pass

    def update_record(self, domain: str, old_answer: str, new_answer: str) -> bool:
        """Update an existing DNS record. Default implementation: delete + add."""
        if self.delete_record(domain, old_answer):
            return self.add_record(domain, new_answer)
        return False


class AdGuardDNSProvider(DNSProvider):
    """AdGuard Home DNS provider implementation."""

    def __init__(self, url: str, username: str, password: str):
        self._url = url.rstrip("/")
        self._auth = HTTPBasicAuth(username, password) if username and password else None
        self._session = requests.Session()
        if self._auth:
            self._session.auth = self._auth

    @property
    def name(self) -> str:
        return "AdGuard Home"

    def test_connection(self) -> bool:
        try:
            response = self._session.get(f"{self._url}/control/status", timeout=5)
            response.raise_for_status()
            logger.info(f"{self.name} connection successful")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to {self.name}: {e}")
            return False

    def get_records(self) -> List[DNSRecord]:
        try:
            response = self._session.get(f"{self._url}/control/rewrite/list", timeout=5)
            response.raise_for_status()
            data = response.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.error(f"Failed to get records from {self.name}: {e}")
            return []

        records = []
        for r in data:
            domain = r.get("domain") if isinstance(r, dict) else None
            answer = r.get("answer") if isinstance(r, dict) else None
            if not isinstance(domain, str) or not isinstance(answer, str):
                logger.warning(f"Skipping malformed record: {r}")
                continue
            records.append(DNSRecord(domain=domain, answer=answer))
        return records

    def add_record(self, domain: str, answer: str) -> bool:
        try:
            data = {"domain": domain, "answer": answer}
            response = self._session.post(f"{self._url}/control/rewrite/add", json=data, timeout=5)
            response.raise_for_status()
            logger.info(f"Added DNS record: {domain} -> {answer}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add record for {domain}: {e}")
            return False

    def delete_record(self, domain: str, answer: str) -> bool:
        try:
            data = {"domain": domain, "answer": answer}
            response = self._session.post(
                f"{self._url}/control/rewrite/delete", json=data, timeout=5
            )
            response.raise_for_status()
            logger.info(f"Deleted DNS record: {domain} -> {answer}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to delete record for {domain}: {e}")
            return False


# =============================================================================
# Reverse Proxy Provider Interface and Implementations
# =============================================================================


class ReverseProxyProvider(ABC):
    """Abstract base class for reverse proxy providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name for logging."""
        pass

    @abstractmethod
    def get_instances(self) -> List[ProxyInstance]:
        """Get configured proxy instances."""
        pass

    @abstractmethod
    def get_routes(self, instance: ProxyInstance) -> List[ProxyRoute]:
        """Get all routes from a proxy instance."""
        pass


class TraefikProxyProvider(ReverseProxyProvider):
    """Traefik reverse proxy provider implementation."""

    HOST_RULE_RE = re.compile(r"Host\([`\"\']([^`\"\']+)[`\"\']\)")
    ZONE_SUFFIX_RE = re.compile(r"-(internal|external)(?:@|$)", re.IGNORECASE)

    def __init__(
        self,
        config_path: str = "",
        instances_json: str = "",
        url: str = "",
        target_ip: str = "",
        timeout_seconds: float = 5.0,
        default_zone: str = "internal",
        zone_label: str = "external-dns.zone",
    ):
        self._config_path = config_path
        self._instances_json = instances_json
        self._url = url
        self._target_ip = target_ip
        self._timeout = timeout_seconds
        self._default_zone = DNSZone.INTERNAL if default_zone != "external" else DNSZone.EXTERNAL
        self._zone_label = zone_label

    @property
    def name(self) -> str:
        return "Traefik"

    def get_instances(self) -> List[ProxyInstance]:
        # Try loading from YAML config file(s) first
        if self._config_path:
            config_files = find_config_files(self._config_path)
            if config_files:
                all_instances: List[ProxyInstance] = []

                for config_file in config_files:
                    try:
                        with open(config_file, "r") as f:
                            config_data = yaml.safe_load(f)

                        if not config_data or "instances" not in config_data:
                            logger.warning(f"Config file {config_file} missing 'instances' key")
                            continue

                        for item in config_data["instances"]:
                            if not isinstance(item, dict):
                                continue
                            name = str(item.get("name") or "traefik").strip()
                            url = str(item.get("url") or "").strip()
                            target_ip = str(
                                item.get("target_ip") or item.get("internal_ip") or ""
                            ).strip()
                            if not url or not target_ip:
                                continue
                            instance_type = str(item.get("type") or "traefik").strip()
                            verify_tls = _parse_bool(item.get("verify_tls"), default=True)
                            username = str(item.get("username") or "").strip()
                            password = str(item.get("password") or "").strip()
                            router_filter = str(item.get("router_filter") or "").strip()
                            middleware_filter = str(item.get("middleware_filter") or "").strip()
                            all_instances.append(
                                ProxyInstance(
                                    name=name,
                                    url=url,
                                    target_ip=target_ip,
                                    type=instance_type,
                                    verify_tls=verify_tls,
                                    username=username,
                                    password=password,
                                    router_filter=router_filter,
                                    middleware_filter=middleware_filter,
                                )
                            )
                    except Exception as e:
                        logger.error(f"Failed to load config from {config_file}: {e}")

                if all_instances:
                    logger.info(
                        f"Loaded {len(all_instances)} Traefik instance(s) from {len(config_files)} config file(s)"
                    )
                    return all_instances

        # Fall back to JSON from environment variable
        if self._instances_json:
            try:
                raw = json.loads(self._instances_json)
                if not isinstance(raw, list):
                    raise ValueError("TRAEFIK_INSTANCES must be a JSON list")

                instances: List[ProxyInstance] = []
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "traefik").strip()
                    url = str(item.get("url") or "").strip()
                    target_ip = str(item.get("target_ip") or item.get("internal_ip") or "").strip()
                    if not url or not target_ip:
                        continue
                    instance_type = str(item.get("type") or "traefik").strip()
                    verify_tls = _parse_bool(item.get("verify_tls"), default=True)
                    username = str(item.get("username") or "").strip()
                    password = str(item.get("password") or "").strip()
                    router_filter = str(item.get("router_filter") or "").strip()
                    middleware_filter = str(item.get("middleware_filter") or "").strip()
                    instances.append(
                        ProxyInstance(
                            name=name,
                            url=url,
                            target_ip=target_ip,
                            type=instance_type,
                            verify_tls=verify_tls,
                            username=username,
                            password=password,
                            router_filter=router_filter,
                            middleware_filter=middleware_filter,
                        )
                    )
                return instances
            except Exception as e:
                logger.error(f"Failed to parse TRAEFIK_INSTANCES JSON: {e}")
                return []

        # Single-instance fallback
        url = self._url.strip()
        target_ip = self._target_ip.strip()
        if not url or not target_ip:
            return []
        return [ProxyInstance(name="traefik", url=url, target_ip=target_ip)]

    def get_routes(self, instance: ProxyInstance) -> List[ProxyRoute]:
        session = requests.Session()
        if instance.username and instance.password:
            session.auth = HTTPBasicAuth(instance.username, instance.password)

        base = instance.url.rstrip("/")
        try:
            response = session.get(
                f"{base}/api/http/routers",
                timeout=self._timeout,
                verify=instance.verify_tls,
            )
            response.raise_for_status()
            routers = response.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.error(f"Failed to get routes from {instance.name}: {e}")
            raise

        # Validate routers is a list
        if not isinstance(routers, list):
            logger.error(
                f"Unexpected response format from {instance.name}: "
                f"expected list, got {type(routers).__name__}"
            )
            return []

        routes: List[ProxyRoute] = []
        for router in routers:
            if not isinstance(router, dict):
                logger.debug(f"Skipping non-dict router entry: {router}")
                continue
            router_name = router.get("name") or ""

            # Apply router name filter if specified
            if instance.router_filter and not self._matches_filter(
                router_name, instance.router_filter
            ):
                logger.debug(
                    f"Router '{router_name}' filtered out by name pattern '{instance.router_filter}'"
                )
                continue

            # Apply middleware filter if specified
            if instance.middleware_filter and not self._has_middleware(
                router, instance.middleware_filter
            ):
                logger.debug(
                    f"Router '{router_name}' filtered out by middleware '{instance.middleware_filter}'"
                )
                continue

            rule = router.get("rule") or ""
            zone = self._detect_zone(router_name, router)

            for hostname in self._extract_hostnames(rule):
                routes.append(
                    ProxyRoute(
                        hostname=hostname,
                        source_name=instance.name,
                        target_ip=instance.target_ip,
                        zone=zone,
                        router_name=router_name,
                    )
                )
        return routes

    def _detect_zone(self, router_name: str, router: Dict[str, Any]) -> DNSZone:
        """Detect DNS zone from router name suffix or labels.

        Priority:
          1. Router name suffix: -internal or -external
          2. Custom label (e.g., external-dns.zone)
          3. Default zone
        """
        # Check router name suffix (e.g., "myapp-internal@docker")
        if router_name:
            match = self.ZONE_SUFFIX_RE.search(router_name)
            if match:
                zone_str = match.group(1).lower()
                return DNSZone.EXTERNAL if zone_str == "external" else DNSZone.INTERNAL

        # Check for zone label in middleware or service metadata
        # Traefik API doesn't expose container labels directly, but we can
        # check if the router name contains zone hints
        # For more advanced label detection, consider using Docker API directly

        return self._default_zone

    def _matches_filter(self, router_name: str, pattern: str) -> bool:
        """Check if router name matches the filter pattern.

        Supports wildcards (* and ?) using fnmatch.
        Example patterns: "*-internal", "app-*", "*-public-*"
        """
        if not pattern:
            return True
        return fnmatch.fnmatch(router_name, pattern)

    def _has_middleware(self, router: Dict[str, Any], middleware_name: str) -> bool:
        """Check if router has the specified middleware.

        Args:
            router: Router object from Traefik API
            middleware_name: Name of middleware to look for

        Returns:
            True if router uses the specified middleware
        """
        if not middleware_name:
            return True

        # Check middlewares list
        middlewares = router.get("middlewares", [])
        if not isinstance(middlewares, list):
            return False

        # Check if any middleware matches (case-insensitive, supports @provider suffix)
        middleware_name_lower = middleware_name.lower()
        for mw in middlewares:
            if not isinstance(mw, str):
                continue
            # Strip @provider suffix for comparison
            mw_base = mw.split("@")[0].lower()
            if mw_base == middleware_name_lower:
                return True

        return False

    def _extract_hostnames(self, rule: str) -> List[str]:
        """Extract hostnames from a Traefik router rule."""
        return sorted({m.group(1) for m in self.HOST_RULE_RE.finditer(rule or "")})


# =============================================================================
# Provider Registry
# =============================================================================


def create_dns_provider() -> DNSProvider:
    """Factory function to create the configured DNS provider."""
    if DNS_PROVIDER == "adguard":
        return AdGuardDNSProvider(ADGUARD_URL, ADGUARD_USERNAME, ADGUARD_PASSWORD)
    else:
        raise ValueError(
            f"Unsupported DNS provider: '{DNS_PROVIDER}'. Supported providers: adguard"
        )


def create_proxy_provider() -> ReverseProxyProvider:
    """Factory function to create the configured reverse proxy provider."""
    if PROXY_PROVIDER == "traefik":
        return TraefikProxyProvider(
            config_path=TRAEFIK_CONFIG_PATH,
            instances_json=TRAEFIK_INSTANCES,
            url=TRAEFIK_URL,
            target_ip=TRAEFIK_TARGET_IP,
            default_zone=EXTERNAL_DNS_DEFAULT_ZONE,
            zone_label=EXTERNAL_DNS_ZONE_LABEL,
        )
    else:
        raise ValueError(
            f"Unsupported reverse proxy provider: '{PROXY_PROVIDER}'. Supported providers: traefik"
        )


# =============================================================================
# Utility Functions
# =============================================================================


def _parse_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_exclude_patterns(value: str) -> List[re.Pattern]:
    """Parse domain exclusion patterns from env var."""
    patterns: List[re.Pattern] = []
    if not value:
        return patterns

    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue

        try:
            if item.startswith("~"):
                # Explicit regex pattern
                regex_str = item[1:]
                patterns.append(re.compile(regex_str, re.IGNORECASE))
            elif "*" in item or "?" in item:
                # Wildcard pattern - convert fnmatch to regex
                regex_str = re.escape(item)
                regex_str = regex_str.replace(r"\*", ".*").replace(r"\?", ".")
                regex_str = f"^{regex_str}$"
                patterns.append(re.compile(regex_str, re.IGNORECASE))
            else:
                # Exact match
                patterns.append(re.compile(f"^{re.escape(item)}$", re.IGNORECASE))
            logger.debug(f"Added exclusion pattern: {item}")
        except re.error as e:
            logger.warning(f"Invalid exclusion pattern '{item}': {e}")

    return patterns


def _is_domain_excluded(domain: str, patterns: List[re.Pattern]) -> bool:
    """Check if a domain matches any exclusion pattern."""
    for pattern in patterns:
        if pattern.search(domain):
            return True
    return False


def _parse_static_rewrites(value: str, default_ip: str) -> Dict[str, str]:
    """Parse static rewrites from env var."""
    parsed: Dict[str, str] = {}
    if not value:
        return parsed

    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue

        if "=" in item:
            domain, answer = item.split("=", 1)
            domain = domain.strip()
            answer = answer.strip()
            if not domain:
                continue
            if not answer or answer.lower() == "true":
                parsed[domain] = default_ip
            else:
                parsed[domain] = answer
        else:
            parsed[item] = default_ip

    return {domain: answer for domain, answer in parsed.items() if domain and answer}


# =============================================================================
# State Management
# =============================================================================


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "instances": {}, "domains": {}}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to load state file {self.path}: {e}")
            return {"version": 1, "instances": {}, "domains": {}}

    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")
        tmp_path.replace(self.path)


# =============================================================================
# Core Syncer
# =============================================================================


class ExternalDNSSyncer:
    def __init__(
        self,
        *,
        dns_provider: DNSProvider,
        proxy_provider: ReverseProxyProvider,
        state_store: StateStore,
        static_rewrites: Dict[str, str],
        exclude_patterns: List[re.Pattern],
    ):
        self.dns_provider = dns_provider
        self.proxy_provider = proxy_provider
        self.state_store = state_store
        self.static_rewrites = static_rewrites
        self.exclude_patterns = exclude_patterns
        self._startup_cleanup_done = False

    def _sync_static_rewrites(self) -> None:
        if not self.static_rewrites:
            return

        current_records = {r.domain: r.answer for r in self.dns_provider.get_records()}

        for domain, answer in self.static_rewrites.items():
            if domain in current_records:
                if current_records[domain] != answer:
                    logger.info(
                        f"Updating static rewrite {domain}: {current_records[domain]} -> {answer}"
                    )
                    self.dns_provider.update_record(domain, current_records[domain], answer)
            else:
                logger.info(f"Adding static rewrite {domain} -> {answer}")
                self.dns_provider.add_record(domain, answer)

    def _cleanup_removed_instances(
        self, state: Dict[str, Any], instances: List[ProxyInstance]
    ) -> None:
        """Remove all DNS records from proxy instances that are no longer configured."""
        configured_names = {i.name for i in instances}
        state_instances = state.get("instances", {})
        removed_instances = set(state_instances.keys()) - configured_names

        if not removed_instances:
            return

        logger.info(f"Detected removed proxy instances: {', '.join(sorted(removed_instances))}")

        # Get current DNS records for cleanup
        all_records = self.dns_provider.get_records()
        records_by_domain: Dict[str, List[str]] = {}
        for r in all_records:
            records_by_domain.setdefault(r.domain, []).append(r.answer)

        # Find and remove domains that were exclusively owned by removed instances
        domains_to_cleanup: List[str] = []
        for domain, domain_state in list(state.get("domains", {}).items()):
            sources = domain_state.get("sources", {})
            if not sources:
                continue

            # Remove the removed instances from this domain's sources
            for removed_name in removed_instances:
                if removed_name in sources:
                    del sources[removed_name]
                    logger.debug(f"Removed source '{removed_name}' from domain '{domain}'")

            # If no sources remain, mark for cleanup
            if not sources:
                domains_to_cleanup.append(domain)

        # Delete DNS records for domains with no remaining sources
        for domain in sorted(domains_to_cleanup):
            # Don't remove static rewrites
            if domain in self.static_rewrites:
                logger.debug(f"Skipping static rewrite '{domain}' during instance cleanup")
                continue

            for answer in records_by_domain.get(domain, []):
                logger.info(f"Removing orphaned record from removed instance: {domain} -> {answer}")
                self.dns_provider.delete_record(domain, answer)

            # Remove from state
            state["domains"].pop(domain, None)

        # Remove the instance entries from state
        for removed_name in removed_instances:
            state["instances"].pop(removed_name, None)
            logger.info(f"Cleaned up state for removed instance: {removed_name}")

    def sync_once(self) -> None:
        now = int(time.time())
        state = self.state_store.load()
        state.setdefault("version", 1)
        state.setdefault("instances", {})
        state.setdefault("domains", {})

        instances = self.proxy_provider.get_instances()

        # On first sync after startup, clean up records from removed proxy instances
        if not self._startup_cleanup_done:
            self._cleanup_removed_instances(state, instances)
            self._startup_cleanup_done = True

        # Ensure static rewrites first.
        self._sync_static_rewrites()

        instance_success: Dict[str, bool] = {}
        instance_seen_domains: Dict[str, Set[str]] = {}

        for instance in instances:
            try:
                routes = self.proxy_provider.get_routes(instance)

                seen: Set[str] = set()
                excluded_count = 0
                external_count = 0
                for route in routes:
                    hostname = route.hostname
                    # Skip domains matching exclusion patterns
                    if _is_domain_excluded(hostname, self.exclude_patterns):
                        excluded_count += 1
                        logger.debug(f"Excluding domain '{hostname}' (matches exclusion pattern)")
                        continue
                    # Skip external zone domains (handled by upstream DNS)
                    if route.zone == DNSZone.EXTERNAL:
                        external_count += 1
                        logger.debug(
                            f"Skipping external zone domain '{hostname}' "
                            f"(router: {route.router_name}, forwarded to upstream DNS)"
                        )
                        continue
                    seen.add(hostname)
                    domain_state = state["domains"].setdefault(hostname, {"sources": {}})
                    sources = domain_state.setdefault("sources", {})
                    sources[instance.name] = {
                        "answer": route.target_ip,
                        "last_seen": now,
                    }

                instance_success[instance.name] = True
                instance_seen_domains[instance.name] = seen
                state["instances"][instance.name] = {
                    "last_success": now,
                    "last_error": "",
                    "url": instance.url,
                }
                stats_parts = []
                if excluded_count:
                    stats_parts.append(f"{excluded_count} excluded")
                if external_count:
                    stats_parts.append(f"{external_count} external")
                stats_msg = f" ({', '.join(stats_parts)})" if stats_parts else ""
                logger.info(
                    f"Proxy instance '{instance.name}': {len(seen)} internal domains{stats_msg}"
                )

            except requests.exceptions.RequestException as e:
                instance_success[instance.name] = False
                instance_seen_domains[instance.name] = set()
                prev = state["instances"].get(instance.name, {})
                state["instances"][instance.name] = {
                    "last_success": prev.get("last_success", 0),
                    "last_error": str(e),
                    "url": instance.url,
                }
                logger.warning(f"Proxy instance '{instance.name}' unreachable: {e}")

        # Prune sources ONLY for instances that were successfully polled.
        domains_to_delete_from_state: List[str] = []
        for domain, domain_state in list(state["domains"].items()):
            sources: Dict[str, Any] = domain_state.get("sources", {})
            if not isinstance(sources, dict):
                sources = {}
                domain_state["sources"] = sources

            for instance in instances:
                if not instance_success.get(instance.name, False):
                    continue
                if instance.name not in sources:
                    continue
                if domain not in instance_seen_domains.get(instance.name, set()):
                    # Confirmed absent on this proxy instance.
                    del sources[instance.name]

            if not sources:
                domains_to_delete_from_state.append(domain)

        # Compute desired global records (one answer per domain).
        desired: Dict[str, str] = {}
        for domain, domain_state in state["domains"].items():
            sources: Dict[str, Any] = domain_state.get("sources", {})
            if not sources:
                continue

            # Pick the answer from the first instance in configured order.
            chosen_answer: Optional[str] = None
            chosen_source: Optional[str] = None
            for instance in instances:
                src = sources.get(instance.name)
                if src and src.get("answer"):
                    chosen_answer = str(src["answer"])
                    chosen_source = instance.name
                    break

            if not chosen_answer:
                continue

            # Log conflicts if multiple instances disagree.
            distinct_answers = sorted(
                {str(v.get("answer")) for v in sources.values() if v.get("answer")}
            )
            if len(distinct_answers) > 1:
                logger.warning(
                    f"Domain '{domain}' present on multiple proxy instances with different target IPs {distinct_answers}; "
                    f"using '{chosen_answer}' from '{chosen_source}'"
                )

            desired[domain] = chosen_answer

        all_records = self.dns_provider.get_records()

        # Build a mapping of domain -> list of answers (to detect duplicates)
        records_by_domain: Dict[str, List[str]] = {}
        for r in all_records:
            records_by_domain.setdefault(r.domain, []).append(r.answer)

        # Clean up existing DNS records that match exclusion patterns
        if self.exclude_patterns:
            for domain, answers in list(records_by_domain.items()):
                # Skip static rewrites
                if domain in self.static_rewrites:
                    continue
                if _is_domain_excluded(domain, self.exclude_patterns):
                    for answer in answers:
                        logger.info(f"Removing excluded domain from DNS: {domain} -> {answer}")
                        self.dns_provider.delete_record(domain, answer)
                    # Also remove from state if present
                    state["domains"].pop(domain, None)
                    # Remove from records_by_domain so we don't process it later
                    del records_by_domain[domain]

        # Apply creates/updates, handling duplicates.
        for domain, answer in sorted(desired.items()):
            existing_answers = records_by_domain.get(domain, [])

            if not existing_answers:
                # No existing record - add it
                logger.info(f"Adding record {domain} -> {answer}")
                self.dns_provider.add_record(domain, answer)
            elif len(existing_answers) == 1 and existing_answers[0] == answer:
                # Exactly one record with correct answer - nothing to do
                pass
            else:
                # Either wrong answer(s) or duplicates exist - clean up and recreate
                if len(existing_answers) > 1:
                    logger.warning(
                        f"Found {len(existing_answers)} duplicate records for {domain}, consolidating"
                    )
                # Delete all existing entries
                for old_answer in existing_answers:
                    self.dns_provider.delete_record(domain, old_answer)
                # Re-add the single correct record
                self.dns_provider.add_record(domain, answer)

        # Apply deletions for domains that now have no sources AND were confirmed absent.
        for domain in sorted(domains_to_delete_from_state):
            # Static rewrites are intentionally not auto-removed.
            if domain in self.static_rewrites:
                continue

            # Delete all records for this domain (handles duplicates too)
            for old_answer in records_by_domain.get(domain, []):
                self.dns_provider.delete_record(domain, old_answer)
            state["domains"].pop(domain, None)

        self.state_store.save(state)


# =============================================================================
# Main
# =============================================================================


def validate_config() -> bool:
    """Validate configuration."""
    errors = []

    # Validate DNS provider config
    if DNS_PROVIDER == "adguard":
        if not ADGUARD_URL:
            errors.append("ADGUARD_URL is required when DNS_PROVIDER=adguard")
        if not ADGUARD_USERNAME or not ADGUARD_PASSWORD:
            logger.warning("⚠️  ADGUARD_USERNAME/PASSWORD not set. Using unauthenticated access.")
    else:
        errors.append(f"Unsupported DNS_PROVIDER: {DNS_PROVIDER}. Supported: adguard")

    # Validate proxy provider config
    if PROXY_PROVIDER == "traefik":
        try:
            provider = create_proxy_provider()
            instances = provider.get_instances()
            if not instances:
                errors.append(
                    "At least one Traefik instance is required "
                    "(set TRAEFIK_INSTANCES or TRAEFIK_URL + TRAEFIK_TARGET_IP/INTERNAL_IP)"
                )
        except Exception as e:
            errors.append(f"Failed to configure Traefik provider: {e}")
    else:
        errors.append(f"Unsupported PROXY_PROVIDER: {PROXY_PROVIDER}. Supported: traefik")

    if errors:
        for error in errors:
            logger.error(error)
        return False

    return True


def main():
    """Main entry point."""
    logger.info(f"external-dns: {PROXY_PROVIDER} -> {DNS_PROVIDER}")

    # Validate configuration
    if not validate_config():
        logger.error("Configuration validation failed")
        sys.exit(1)

    # Create providers
    dns_provider = create_dns_provider()
    proxy_provider = create_proxy_provider()
    instances = proxy_provider.get_instances()

    logger.info(f"DNS Provider: {dns_provider.name}")
    logger.info(f"Proxy Provider: {proxy_provider.name}")
    logger.info(f"Proxy instances: {', '.join([i.name for i in instances])}")
    logger.info(
        f"Default zone: {EXTERNAL_DNS_DEFAULT_ZONE} (only 'internal' zones sync to local DNS)"
    )
    logger.info(f"Sync mode: {SYNC_MODE}")
    if SYNC_MODE == "watch":
        logger.info(f"Poll interval: {POLL_INTERVAL_SECONDS}s")
        config_files = find_config_files(TRAEFIK_CONFIG_PATH)
        if len(config_files) > 1:
            logger.info(
                f"Config watch: scanning {len(config_files)} files in {TRAEFIK_CONFIG_PATH}"
            )
        else:
            logger.info(f"Config watch: enabled for {TRAEFIK_CONFIG_PATH}")

    # Best-effort default IP for static rewrites (use first instance target_ip).
    default_ip_for_static = instances[0].target_ip if instances else ""
    static_rewrites = _parse_static_rewrites(EXTERNAL_DNS_STATIC_REWRITES, default_ip_for_static)
    if static_rewrites:
        logger.info(f"Static rewrites: {', '.join(sorted(static_rewrites.keys()))}")

    # Parse domain exclusion patterns
    exclude_patterns = _parse_exclude_patterns(EXTERNAL_DNS_EXCLUDE_DOMAINS)
    if exclude_patterns:
        logger.info(f"Domain exclusions: {len(exclude_patterns)} pattern(s) configured")

    # Test connection
    if not dns_provider.test_connection():
        logger.error(f"Cannot connect to {dns_provider.name}. Exiting.")
        sys.exit(1)

    syncer = ExternalDNSSyncer(
        dns_provider=dns_provider,
        proxy_provider=proxy_provider,
        state_store=StateStore(STATE_PATH),
        static_rewrites=static_rewrites,
        exclude_patterns=exclude_patterns,
    )

    # Run sync
    try:
        if SYNC_MODE == "once":
            syncer.sync_once()
            return

        if SYNC_MODE != "watch":
            logger.error(f"Invalid SYNC_MODE: {SYNC_MODE}. Use 'once' or 'watch'")
            sys.exit(1)

        # Track all config files modification times for auto-reload
        config_files = find_config_files(TRAEFIK_CONFIG_PATH)
        last_config_mtimes = get_config_files_mtimes(config_files)

        # Polling loop with config file watching
        while True:
            syncer.sync_once()

            # Check for new config files or changes to existing ones
            current_config_files = find_config_files(TRAEFIK_CONFIG_PATH)
            current_mtimes = get_config_files_mtimes(current_config_files)

            # Detect changes: new files, deleted files, or modified files
            files_changed = (
                set(current_config_files) != set(config_files)
                or current_mtimes != last_config_mtimes
            )

            if files_changed:
                changed_files = []
                if set(current_config_files) != set(config_files):
                    new_files = set(current_config_files) - set(config_files)
                    removed_files = set(config_files) - set(current_config_files)
                    if new_files:
                        logger.info(
                            f"New config file(s) detected: {', '.join([Path(f).name for f in new_files])}"
                        )
                    if removed_files:
                        logger.info(
                            f"Config file(s) removed: {', '.join([Path(f).name for f in removed_files])}"
                        )
                    changed_files = list(new_files) + list(removed_files)
                else:
                    for f in current_config_files:
                        if current_mtimes.get(f, 0) != last_config_mtimes.get(f, 0):
                            changed_files.append(f)

                if changed_files:
                    logger.info(
                        f"Config change detected in: {', '.join([Path(f).name for f in changed_files])}"
                    )

                config_files = current_config_files
                last_config_mtimes = current_mtimes

                # Recreate proxy provider with new config
                try:
                    proxy_provider = create_proxy_provider()
                    instances = proxy_provider.get_instances()
                    logger.info(
                        f"Reloaded {len(instances)} instance(s): {', '.join([i.name for i in instances])}"
                    )

                    # Update syncer with new provider
                    syncer.proxy_provider = proxy_provider

                    # Trigger immediate sync after config reload
                    logger.info("Triggering immediate sync after config reload")
                    syncer.sync_once()
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}", exc_info=True)
                    logger.warning("Continuing with previous configuration")

            time.sleep(max(5, POLL_INTERVAL_SECONDS))

    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
