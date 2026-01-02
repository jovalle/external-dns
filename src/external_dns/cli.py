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

Configuration:

    All configuration is done via a YAML config file. Environment variables are
    supported as fallback for backwards compatibility.

    Config file location:
        CONFIG_PATH    Path to YAML config file (default: /config/config.yaml)

    Example config file:

        # DNS providers - where DNS records are written
        providers:
          - name: adguard-home
            provider: adguard  # Provider type: adguard (default)
            url: "http://adguard:3000"
            username: "admin"
            password: "secret"

        # Sources - reverse proxy instances to discover routes from
        sources:
          - name: "core"
            url: "http://traefik:8080"
            target_ip: "10.0.0.2"
            verify_tls: true
            router_filter: "*-internal"
          - name: "edge"
            url: "https://traefik2:8080"
            target_ip: "10.0.0.3"
            verify_tls: false

    DNS Provider Configuration (providers section):
        name            Friendly name for this provider instance
        provider        Provider type: "adguard" (default)
        url             Provider API URL (required)
        username        API username (optional, for basic auth)
        password        API password (optional, for basic auth)

    Source Configuration (sources section):
        name            Friendly name for this source
        type            Source type: "traefik" (default)
        url             API URL (required)
        target_ip       IP address to use for DNS records (required)
        verify_tls      Verify TLS certificates (default: true)
        router_filter   Wildcard pattern to filter routers (e.g., "*-internal")
        middleware_filter  Filter by middleware name

    Runtime Environment Variables:
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
                                    - Regex (prefix with ~): "~^staging-\\d+\\.example\\.com$"
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
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar

import requests
import yaml
from requests.auth import HTTPBasicAuth

T = TypeVar("T")

# =============================================================================
# Retry Utilities
# =============================================================================


def retry_with_backoff(
    func: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (requests.exceptions.RequestException,),
) -> T:
    """Retry a function with exponential backoff.

    Args:
        func: Zero-argument callable to retry
        max_retries: Maximum number of retry attempts (0 = no retries)
        base_delay: Initial delay between retries in seconds
        max_delay: Maximum delay cap in seconds
        exponential_base: Base for exponential backoff calculation
        retryable_exceptions: Tuple of exception types that trigger retry

    Returns:
        Result of successful function call

    Raises:
        Last exception if all retries exhausted
    """
    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return func()
        except retryable_exceptions as e:
            last_exception = e
            if attempt == max_retries:
                break
            delay = min(base_delay * (exponential_base**attempt), max_delay)
            logger.debug(f"Retry {attempt + 1}/{max_retries} after {delay:.1f}s: {e}")
            time.sleep(delay)

    raise last_exception  # type: ignore[misc]


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


@dataclass
class DNSProviderConfig:
    """Configuration for a DNS provider."""

    name: str
    provider: str  # adguard, cloudflare, etc.
    url: str
    username: str = ""
    password: str = ""
    api_token: str = ""  # For providers that use API tokens


def load_dns_providers_from_yaml(config_path: str) -> List[DNSProviderConfig]:
    """Load DNS provider configurations from YAML config file.

    Supports two formats:
    - New format: 'providers' list with 'provider' field for type
    - Legacy format: 'dns_provider' single dict (backwards compatible)

    Args:
        config_path: Path to config file or directory

    Returns:
        List of DNSProviderConfig objects
    """
    config_files = find_config_files(config_path)
    if not config_files:
        return []

    providers: List[DNSProviderConfig] = []

    for config_file in config_files:
        try:
            with open(config_file, "r") as f:
                config_data = yaml.safe_load(f)

            if not config_data:
                continue

            # New format: 'providers' list
            if "providers" in config_data:
                providers_list = config_data["providers"]
                if isinstance(providers_list, list):
                    for item in providers_list:
                        if not isinstance(item, dict):
                            continue
                        provider_type = str(item.get("provider") or "adguard").strip().lower()
                        name = str(item.get("name") or provider_type).strip()
                        url = str(item.get("url") or "").strip()
                        if not url:
                            continue
                        providers.append(
                            DNSProviderConfig(
                                name=name,
                                provider=provider_type,
                                url=url,
                                username=str(item.get("username") or "").strip(),
                                password=str(item.get("password") or "").strip(),
                                api_token=str(item.get("api_token") or "").strip(),
                            )
                        )

            # Legacy format: 'dns_provider' single dict
            elif "dns_provider" in config_data:
                dns_config = config_data["dns_provider"]
                if isinstance(dns_config, dict):
                    url = str(dns_config.get("url") or "").strip()
                    if url:
                        providers.append(
                            DNSProviderConfig(
                                name="default",
                                provider="adguard",
                                url=url,
                                username=str(dns_config.get("username") or "").strip(),
                                password=str(dns_config.get("password") or "").strip(),
                            )
                        )

        except Exception as e:
            logger.debug(f"Failed to parse DNS config from {config_file}: {e}")
            continue

    return providers


def load_dns_config_from_yaml(config_path: str) -> Optional[Dict[str, str]]:
    """Load DNS provider configuration from YAML config file.

    Looks for 'providers' (new) or 'dns_provider' (legacy) section.

    Args:
        config_path: Path to config file or directory

    Returns:
        Dict with url, username, password if found, None otherwise
    """
    providers = load_dns_providers_from_yaml(config_path)
    if not providers:
        return None

    # Return first provider for backwards compatibility
    p = providers[0]
    return {
        "url": p.url,
        "username": p.username,
        "password": p.password,
    }


@dataclass
class RuntimeSettings:
    """Runtime configuration settings."""

    sync_mode: str = "watch"
    poll_interval: int = 60
    log_level: str = "INFO"
    default_zone: str = "internal"
    exclude_domains: List[str] = None  # type: ignore
    static_rewrites: Dict[str, str] = None  # type: ignore

    def __post_init__(self):
        if self.exclude_domains is None:
            self.exclude_domains = []
        if self.static_rewrites is None:
            self.static_rewrites = {}


def load_settings_from_yaml(config_path: str) -> RuntimeSettings:
    """Load runtime settings from YAML config file.

    Env vars take priority over YAML config values.

    Args:
        config_path: Path to config file or directory

    Returns:
        RuntimeSettings with merged values (env vars override YAML)
    """
    settings = RuntimeSettings()
    config_files = find_config_files(config_path)

    # Load from YAML first
    for config_file in config_files:
        try:
            with open(config_file, "r") as f:
                config_data = yaml.safe_load(f)

            if not config_data:
                continue

            # Load settings section
            if "settings" in config_data and isinstance(config_data["settings"], dict):
                s = config_data["settings"]
                if "sync_mode" in s:
                    settings.sync_mode = str(s["sync_mode"]).strip().lower()
                if "poll_interval" in s:
                    settings.poll_interval = int(s["poll_interval"])
                if "log_level" in s:
                    settings.log_level = str(s["log_level"]).strip().upper()
                if "default_zone" in s:
                    settings.default_zone = str(s["default_zone"]).strip().lower()

            # Load exclude_domains list
            if "exclude_domains" in config_data:
                excludes = config_data["exclude_domains"]
                if isinstance(excludes, list):
                    settings.exclude_domains = [str(e).strip() for e in excludes if e]

            # Load static_rewrites dict
            if "static_rewrites" in config_data:
                rewrites = config_data["static_rewrites"]
                if isinstance(rewrites, dict):
                    settings.static_rewrites = {
                        str(k).strip(): str(v).strip() for k, v in rewrites.items() if k
                    }

        except Exception:
            # Log at debug since logger may not be configured yet
            pass  # Will use defaults

    # Env vars override YAML values
    if os.getenv("SYNC_MODE"):
        settings.sync_mode = os.getenv("SYNC_MODE", "watch").strip().lower()
    if os.getenv("POLL_INTERVAL_SECONDS"):
        settings.poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    if os.getenv("LOG_LEVEL"):
        settings.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if os.getenv("EXTERNAL_DNS_DEFAULT_ZONE"):
        settings.default_zone = os.getenv("EXTERNAL_DNS_DEFAULT_ZONE", "internal").strip().lower()

    # Merge exclude domains from env var (append to YAML list)
    env_excludes = os.getenv("EXTERNAL_DNS_EXCLUDE_DOMAINS", "").strip()
    if env_excludes:
        for item in env_excludes.split(","):
            item = item.strip()
            if item and item not in settings.exclude_domains:
                settings.exclude_domains.append(item)

    # Merge static rewrites from env var (override YAML values)
    env_rewrites = os.getenv("EXTERNAL_DNS_STATIC_REWRITES", "").strip()
    if env_rewrites:
        for item in env_rewrites.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                domain, answer = item.split("=", 1)
                settings.static_rewrites[domain.strip()] = answer.strip()
            else:
                # Will use first instance target_ip as default (handled later)
                settings.static_rewrites[item] = ""

    return settings


# =============================================================================
# Configuration
# =============================================================================

# Provider selection
DNS_PROVIDER = os.getenv("DNS_PROVIDER", "adguard").lower().strip()
PROXY_PROVIDER = os.getenv("PROXY_PROVIDER", "traefik").lower().strip()

# AdGuard configuration (env vars as fallback, YAML config takes priority)
ADGUARD_URL = os.getenv("ADGUARD_URL", "")
ADGUARD_USERNAME = os.getenv("ADGUARD_USERNAME", "")
ADGUARD_PASSWORD = os.getenv("ADGUARD_PASSWORD", "")

# Config file path (supports CONFIG_PATH for backwards compatibility)
CONFIG_PATH = os.getenv("CONFIG_PATH", os.getenv("CONFIG_PATH", "/config/config.yaml"))
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

# Shutdown event for graceful termination
_shutdown_event = threading.Event()


def _signal_handler(signum: int, frame: Any) -> None:
    """Handle shutdown signals (SIGTERM, SIGINT) for graceful termination."""
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")
    _shutdown_event.set()


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
        def _do_request() -> bool:
            response = self._session.get(f"{self._url}/control/status", timeout=5)
            response.raise_for_status()
            return True

        try:
            result = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
            logger.info(f"{self.name} connection successful")
            return result
        except requests.exceptions.RequestException as e:
            status_info = ""
            if hasattr(e, "response") and e.response is not None:
                status_info = f" (HTTP {e.response.status_code})"
            logger.error(f"Failed to connect to {self.name} at {self._url}{status_info}: {e}")
            return False

    def get_records(self) -> List[DNSRecord]:
        def _do_request() -> Any:
            response = self._session.get(f"{self._url}/control/rewrite/list", timeout=5)
            response.raise_for_status()
            return response.json()

        try:
            data = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            status_info = ""
            if hasattr(e, "response") and e.response is not None:
                status_info = f" (HTTP {e.response.status_code})"
            logger.error(f"Failed to get records from {self.name} at {self._url}{status_info}: {e}")
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
        def _do_request() -> bool:
            data = {"domain": domain, "answer": answer}
            response = self._session.post(f"{self._url}/control/rewrite/add", json=data, timeout=5)
            response.raise_for_status()
            return True

        try:
            retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
            logger.info(f"Added DNS record: {domain} -> {answer}")
            return True
        except requests.exceptions.RequestException as e:
            status_info = ""
            if hasattr(e, "response") and e.response is not None:
                status_info = f" (HTTP {e.response.status_code})"
            logger.error(f"Failed to add record for {domain} at {self._url}{status_info}: {e}")
            return False

    def delete_record(self, domain: str, answer: str) -> bool:
        def _do_request() -> bool:
            data = {"domain": domain, "answer": answer}
            response = self._session.post(
                f"{self._url}/control/rewrite/delete", json=data, timeout=5
            )
            response.raise_for_status()
            return True

        try:
            retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
            logger.info(f"Deleted DNS record: {domain} -> {answer}")
            return True
        except requests.exceptions.RequestException as e:
            status_info = ""
            if hasattr(e, "response") and e.response is not None:
                status_info = f" (HTTP {e.response.status_code})"
            logger.error(f"Failed to delete record for {domain} at {self._url}{status_info}: {e}")
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

                        if not config_data or "sources" not in config_data:
                            logger.warning(f"Config file {config_file} missing 'sources' key")
                            continue

                        for item in config_data["sources"]:
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

        def _do_request() -> Any:
            response = session.get(
                f"{base}/api/http/routers",
                timeout=self._timeout,
                verify=instance.verify_tls,
            )
            response.raise_for_status()
            return response.json()

        try:
            routers = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
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


def get_dns_config() -> Dict[str, str]:
    """Get DNS provider configuration from YAML config or env vars.

    Priority: YAML config > environment variables

    Returns:
        Dict with url, username, password
    """
    # Try YAML config first
    yaml_config = load_dns_config_from_yaml(CONFIG_PATH)
    if yaml_config:
        return yaml_config

    # Fall back to environment variables
    return {
        "url": ADGUARD_URL,
        "username": ADGUARD_USERNAME,
        "password": ADGUARD_PASSWORD,
    }


def create_dns_provider() -> DNSProvider:
    """Factory function to create the configured DNS provider."""
    supported = ["adguard"]
    if DNS_PROVIDER == "adguard":
        config = get_dns_config()
        return AdGuardDNSProvider(config["url"], config["username"], config["password"])
    else:
        raise ValueError(
            f"Unsupported DNS_PROVIDER: '{DNS_PROVIDER}'. "
            f"Supported providers: {', '.join(supported)}. "
            f"Check your DNS_PROVIDER environment variable."
        )


def create_proxy_provider(default_zone: Optional[str] = None) -> ReverseProxyProvider:
    """Factory function to create the configured reverse proxy provider.

    Args:
        default_zone: Override default zone (from settings or env var)
    """
    supported = ["traefik"]
    zone = default_zone or EXTERNAL_DNS_DEFAULT_ZONE
    if PROXY_PROVIDER == "traefik":
        return TraefikProxyProvider(
            config_path=CONFIG_PATH,
            instances_json=TRAEFIK_INSTANCES,
            url=TRAEFIK_URL,
            target_ip=TRAEFIK_TARGET_IP,
            default_zone=zone,
            zone_label=EXTERNAL_DNS_ZONE_LABEL,
        )
    else:
        raise ValueError(
            f"Unsupported PROXY_PROVIDER: '{PROXY_PROVIDER}'. "
            f"Supported providers: {', '.join(supported)}. "
            f"Check your PROXY_PROVIDER environment variable."
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


def _parse_exclude_patterns(value: Any) -> List[re.Pattern]:
    """Parse domain exclusion patterns from list or comma-separated string."""
    patterns: List[re.Pattern] = []
    if not value:
        return patterns

    # Convert to list if string
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        return patterns

    for item in items:
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

    def _is_record_managed(self, state: Dict[str, Any], domain: str, answer: str) -> bool:
        """Check if a DNS record was created by external-dns."""
        managed = state.get("managed_records", {})
        return answer in managed.get(domain, [])

    def _mark_record_managed(self, state: Dict[str, Any], domain: str, answer: str) -> None:
        """Track a DNS record as managed by external-dns."""
        managed = state.setdefault("managed_records", {})
        domain_answers = managed.setdefault(domain, [])
        if answer not in domain_answers:
            domain_answers.append(answer)

    def _unmark_record_managed(self, state: Dict[str, Any], domain: str, answer: str) -> None:
        """Remove a DNS record from managed tracking."""
        managed = state.get("managed_records", {})
        if domain in managed:
            if answer in managed[domain]:
                managed[domain].remove(answer)
            if not managed[domain]:
                del managed[domain]

    def _sync_static_rewrites(self, state: Dict[str, Any]) -> None:
        if not self.static_rewrites:
            return

        current_records = {r.domain: r.answer for r in self.dns_provider.get_records()}

        for domain, answer in self.static_rewrites.items():
            if domain in current_records:
                current_answer = current_records[domain]
                if current_answer == answer:
                    # Record already exists with correct answer - mark as managed
                    self._mark_record_managed(state, domain, answer)
                elif self._is_record_managed(state, domain, current_answer):
                    # Record is managed by us with wrong answer - update it
                    logger.info(f"Updating static rewrite {domain}: {current_answer} -> {answer}")
                    self.dns_provider.update_record(domain, current_answer, answer)
                    self._unmark_record_managed(state, domain, current_answer)
                    self._mark_record_managed(state, domain, answer)
                else:
                    # Pre-existing record not managed by us - warn and skip
                    logger.warning(
                        f"Static rewrite {domain} -> {answer} conflicts with pre-existing "
                        f"record {domain} -> {current_answer} (not managed by external-dns, skipping)"
                    )
            else:
                logger.info(f"Adding static rewrite {domain} -> {answer}")
                self.dns_provider.add_record(domain, answer)
                self._mark_record_managed(state, domain, answer)

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

        # Delete DNS records for domains with no remaining sources (only if managed)
        for domain in sorted(domains_to_cleanup):
            # Don't remove static rewrites
            if domain in self.static_rewrites:
                logger.debug(f"Skipping static rewrite '{domain}' during instance cleanup")
                continue

            for answer in records_by_domain.get(domain, []):
                if self._is_record_managed(state, domain, answer):
                    logger.info(
                        f"Removing orphaned record from removed instance: {domain} -> {answer}"
                    )
                    self.dns_provider.delete_record(domain, answer)
                    self._unmark_record_managed(state, domain, answer)
                else:
                    logger.debug(
                        f"Skipping pre-existing record during instance cleanup: {domain} -> {answer}"
                    )

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
        state.setdefault("managed_records", {})

        instances = self.proxy_provider.get_instances()

        # On first sync after startup, clean up records from removed proxy instances
        if not self._startup_cleanup_done:
            self._cleanup_removed_instances(state, instances)
            self._startup_cleanup_done = True

        # Ensure static rewrites first.
        self._sync_static_rewrites(state)

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
                error_detail = str(e)
                if hasattr(e, "response") and e.response is not None:
                    error_detail = f"HTTP {e.response.status_code}: {e}"
                prev = state["instances"].get(instance.name, {})
                state["instances"][instance.name] = {
                    "last_success": prev.get("last_success", 0),
                    "last_error": error_detail,
                    "url": instance.url,
                }
                logger.warning(
                    f"Proxy instance '{instance.name}' ({instance.url}) unreachable: {error_detail}"
                )

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

        # Clean up existing DNS records that match exclusion patterns (only managed records)
        if self.exclude_patterns:
            for domain, answers in list(records_by_domain.items()):
                # Skip static rewrites
                if domain in self.static_rewrites:
                    continue
                if _is_domain_excluded(domain, self.exclude_patterns):
                    deleted_any = False
                    for answer in answers:
                        if self._is_record_managed(state, domain, answer):
                            logger.info(f"Removing excluded domain from DNS: {domain} -> {answer}")
                            self.dns_provider.delete_record(domain, answer)
                            self._unmark_record_managed(state, domain, answer)
                            deleted_any = True
                        else:
                            logger.debug(
                                f"Skipping pre-existing excluded record: {domain} -> {answer}"
                            )
                    # Also remove from state if present
                    state["domains"].pop(domain, None)
                    # Remove from records_by_domain so we don't process it later
                    if deleted_any:
                        del records_by_domain[domain]

        # Apply creates/updates, handling duplicates (respecting managed records).
        for domain, answer in sorted(desired.items()):
            existing_answers = records_by_domain.get(domain, [])

            if not existing_answers:
                # No existing record - add it and mark as managed
                logger.info(f"Adding record {domain} -> {answer}")
                self.dns_provider.add_record(domain, answer)
                self._mark_record_managed(state, domain, answer)
            elif len(existing_answers) == 1 and existing_answers[0] == answer:
                # Exactly one record with correct answer - adopt it as managed
                self._mark_record_managed(state, domain, answer)
            else:
                # Either wrong answer(s) or duplicates exist
                # Check which records we can manage
                managed_answers = [
                    a for a in existing_answers if self._is_record_managed(state, domain, a)
                ]
                unmanaged_answers = [
                    a for a in existing_answers if not self._is_record_managed(state, domain, a)
                ]

                if unmanaged_answers:
                    # There are pre-existing records we didn't create
                    if answer in unmanaged_answers:
                        # Desired answer already exists as pre-existing - adopt it
                        logger.debug(f"Adopting pre-existing record {domain} -> {answer}")
                        self._mark_record_managed(state, domain, answer)
                        # Clean up any managed duplicates
                        for old_answer in managed_answers:
                            if old_answer != answer:
                                logger.info(f"Removing managed duplicate {domain} -> {old_answer}")
                                self.dns_provider.delete_record(domain, old_answer)
                                self._unmark_record_managed(state, domain, old_answer)
                    else:
                        # Pre-existing record(s) with different answer - warn and skip
                        logger.warning(
                            f"Domain {domain} has pre-existing record(s) {unmanaged_answers} "
                            f"(not managed by external-dns); skipping desired {answer}"
                        )
                        # Still clean up our managed records for this domain
                        for old_answer in managed_answers:
                            logger.info(
                                f"Removing obsolete managed record {domain} -> {old_answer}"
                            )
                            self.dns_provider.delete_record(domain, old_answer)
                            self._unmark_record_managed(state, domain, old_answer)
                else:
                    # All records are managed by us - clean up and recreate
                    if len(existing_answers) > 1:
                        logger.warning(
                            f"Found {len(existing_answers)} duplicate records for {domain}, consolidating"
                        )
                    # Delete all existing managed entries
                    for old_answer in existing_answers:
                        self.dns_provider.delete_record(domain, old_answer)
                        self._unmark_record_managed(state, domain, old_answer)
                    # Re-add the single correct record
                    self.dns_provider.add_record(domain, answer)
                    self._mark_record_managed(state, domain, answer)

        # Apply deletions for domains that now have no sources AND were confirmed absent.
        for domain in sorted(domains_to_delete_from_state):
            # Static rewrites are intentionally not auto-removed.
            if domain in self.static_rewrites:
                continue

            # Delete only managed records for this domain
            for old_answer in records_by_domain.get(domain, []):
                if self._is_record_managed(state, domain, old_answer):
                    logger.info(f"Removing record {domain} -> {old_answer}")
                    self.dns_provider.delete_record(domain, old_answer)
                    self._unmark_record_managed(state, domain, old_answer)
                else:
                    logger.debug(f"Preserving pre-existing record {domain} -> {old_answer}")
            state["domains"].pop(domain, None)

        self.state_store.save(state)


# =============================================================================
# Main
# =============================================================================


def validate_config() -> bool:
    """Validate configuration.

    Configuration priority:
    1. YAML config file (providers section or legacy dns_provider)
    2. Environment variables (fallback)
    """
    errors = []

    # Check if YAML config file exists and has valid configuration
    yaml_providers = load_dns_providers_from_yaml(CONFIG_PATH)
    using_yaml_config = len(yaml_providers) > 0

    # Validate DNS provider config
    dns_config = get_dns_config()
    if not dns_config["url"]:
        if using_yaml_config:
            errors.append(
                f"No DNS provider URL found in config file. "
                f"Check 'providers' or 'dns_provider' section in {CONFIG_PATH}"
            )
        else:
            errors.append(
                "DNS provider URL is required. "
                "Set via YAML config (providers[].url) or ADGUARD_URL env var."
            )
    else:
        # Determine provider type from YAML or env
        provider_type = "adguard"
        if yaml_providers:
            provider_type = yaml_providers[0].provider

        if provider_type not in ["adguard"]:
            errors.append(f"Unsupported DNS provider type: {provider_type}. Supported: adguard")
        elif not dns_config["username"] or not dns_config["password"]:
            if using_yaml_config:
                logger.warning(
                    "⚠️  DNS provider username/password not set in config. "
                    "Using unauthenticated access."
                )
            else:
                logger.warning(
                    "⚠️  ADGUARD_USERNAME/PASSWORD not set. Using unauthenticated access."
                )

    # Validate proxy provider config (sources from YAML)
    if PROXY_PROVIDER == "traefik":
        try:
            provider = create_proxy_provider()
            instances = provider.get_instances()
            if not instances:
                config_files = find_config_files(CONFIG_PATH)
                if config_files:
                    errors.append(
                        f"No sources configured in {CONFIG_PATH}. "
                        f"Add at least one source with url and target_ip."
                    )
                else:
                    errors.append(
                        f"Config file not found: {CONFIG_PATH}. "
                        f"Create config file or set TRAEFIK_URL + TRAEFIK_TARGET_IP env vars."
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
    # Load settings from config file (env vars override)
    settings = load_settings_from_yaml(CONFIG_PATH)

    # Reconfigure logging with settings from config
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))

    logger.info(f"external-dns: {PROXY_PROVIDER} -> {DNS_PROVIDER}")

    # Validate configuration
    if not validate_config():
        logger.error("Configuration validation failed")
        sys.exit(1)

    # Create providers
    dns_config = get_dns_config()
    dns_provider = create_dns_provider()
    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
    instances = proxy_provider.get_instances()

    logger.info(f"DNS Provider: {dns_provider.name} ({dns_config['url']})")
    logger.info(f"Proxy Provider: {proxy_provider.name}")
    logger.info(f"Configured {len(instances)} proxy instance(s):")
    for inst in instances:
        logger.info(f"  - {inst.name}: {inst.url} -> {inst.target_ip}")
    logger.info(f"Default zone: {settings.default_zone} (only 'internal' zones sync to local DNS)")
    logger.info(f"Sync mode: {settings.sync_mode}")
    if settings.sync_mode == "watch":
        logger.info(f"Poll interval: {settings.poll_interval}s")
        config_files = find_config_files(CONFIG_PATH)
        if len(config_files) > 1:
            logger.info(f"Config watch: scanning {len(config_files)} files in {CONFIG_PATH}")
        else:
            logger.info(f"Config watch: enabled for {CONFIG_PATH}")

    # Best-effort default IP for static rewrites (use first instance target_ip).
    default_ip_for_static = instances[0].target_ip if instances else ""

    # Process static rewrites from settings (fill in default IP for entries without one)
    static_rewrites: Dict[str, str] = {}
    for domain, answer in settings.static_rewrites.items():
        if answer:
            static_rewrites[domain] = answer
        elif default_ip_for_static:
            static_rewrites[domain] = default_ip_for_static
    if static_rewrites:
        logger.info(f"Static rewrites: {', '.join(sorted(static_rewrites.keys()))}")

    # Parse domain exclusion patterns
    exclude_patterns = _parse_exclude_patterns(settings.exclude_domains)
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

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Run sync
    try:
        if settings.sync_mode == "once":
            syncer.sync_once()
            return

        if settings.sync_mode != "watch":
            logger.error(f"Invalid sync_mode: {settings.sync_mode}. Use 'once' or 'watch'")
            sys.exit(1)

        # Track all config files modification times for auto-reload
        config_files = find_config_files(CONFIG_PATH)
        last_config_mtimes = get_config_files_mtimes(config_files)

        # Cycle counter for health check logging
        cycle_count = 0

        # Polling loop with config file watching
        while not _shutdown_event.is_set():
            cycle_count += 1
            try:
                syncer.sync_once()
            except Exception as e:
                logger.error(f"Sync cycle {cycle_count} failed: {e}", exc_info=True)
                # Continue to next cycle - don't crash the daemon
                # State is preserved from last successful sync

            # Periodic health check logging
            if cycle_count % 10 == 0:
                logger.info(f"Health check: {cycle_count} sync cycles completed")

            # Check for new config files or changes to existing ones
            current_config_files = find_config_files(CONFIG_PATH)
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
                    # Reload settings for any changes
                    settings = load_settings_from_yaml(CONFIG_PATH)
                    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
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

            # Interruptible sleep - will return immediately if shutdown signal received
            _shutdown_event.wait(max(5, settings.poll_interval))

        logger.info("Shutdown complete.")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
