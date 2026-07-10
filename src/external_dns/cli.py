#!/usr/bin/env python3
"""external-dns - Universal DNS Synchronization

Syncs reverse proxy routes into providers, similar in spirit to Kubernetes
external-dns. Providers may publish DNS records or non-DNS route aliases.

Supported Providers (DNS and other):
    - adguard: AdGuard Home DNS rewrites
    - technitium: Technitium DNS Server A records
    - goku: Goku golink aliases

Supported Sources (reverse proxies):
    - traefik: Traefik HTTP routers
    (more coming soon)

Configuration:

    All configuration is done via a YAML config file. Environment variables are
    supported as fallback for backwards compatibility.

    Config file location:
        CONFIG_PATH    Path to YAML config file (default: /config/config.yaml)

    Example config file:

        # Providers - where discovered routes are published
        providers:
          - name: adguard-home
            provider: adguard  # Provider type: adguard (default)
            url: "http://adguard:3000"
            username: "admin"
            password: "secret"
          - name: technitium-primary
            provider: technitium
            url: "https://dns.example.com"
            api_token: "secret-token"
            zones:
              - example.com
              - internal.example.com

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

    Provider Configuration (providers section):
        name            Friendly, unique name for this provider
        provider        Provider type: "adguard", "technitium", or "goku"
        url             Provider API URL (required)
        username        API username (optional, adguard basic auth)
        password        API password (optional, adguard basic auth)
        api_token       API token (technitium/goku; sent as Authorization: Bearer)
        zones           Authoritative zones to manage (technitium, required)

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
        EXTERNAL_DNS_DEFAULT_ZONE     Default zone for routers without explicit suffix.
                                      "internal" (default) or "external".
                                      - internal: Create DNS rewrites in local DNS provider
                                      - external: Skip DNS rewrite (forward to upstream DNS)

        Zone detection priority (first match wins):
          1. Router name suffix: "-internal" or "-external"
          2. Default zone (from EXTERNAL_DNS_DEFAULT_ZONE)

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
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar
from urllib.parse import urlparse

import requests
import yaml
from requests.auth import HTTPBasicAuth

T = TypeVar("T")


def _concise_error(error: Exception) -> str:
    """Return an operator-friendly error without requests/urllib3 wrapper noise."""
    if isinstance(error, requests.exceptions.Timeout):
        return "request timed out"
    if isinstance(error, requests.exceptions.ConnectionError):
        message = str(error).lower()
        if "connection refused" in message:
            return "connection refused"
        if "name or service not known" in message or "nodename nor servname" in message:
            return "host not found"
        return "connection failed"
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        reason = str(error.response.reason or "").strip()
        return f"HTTP {error.response.status_code}{f' {reason}' if reason else ''}"
    if isinstance(error, (requests.exceptions.JSONDecodeError, json.JSONDecodeError)):
        return "invalid JSON response"
    return str(error) or type(error).__name__


def _local_endpoint_hint(url: str, detail: str) -> str:
    """Explain the common loopback-forwarding failure without assuming SSH."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in {"127.0.0.1", "::1", "localhost"} and detail == "connection refused":
        return "; check the local listener or tunnel"
    return ""


def _endpoint_error(url: str, error: Exception) -> str:
    detail = _concise_error(error)
    return f"{detail}{_local_endpoint_hint(url, detail)}"


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


# =============================================================================
# Webhook Utilities
# =============================================================================


def call_webhook(
    url: str,
    *,
    method: str = "POST",
    username: str = "",
    password: str = "",
    timeout: int = 30,
    max_retries: int = 2,
) -> bool:
    """Call a webhook URL with retry logic.

    This is used to trigger external services (e.g., adguardhome-sync) after
    DNS record changes.

    Args:
        url: Webhook URL to call
        method: HTTP method (default: POST)
        username: Optional HTTP Basic Auth username
        password: Optional HTTP Basic Auth password
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts

    Returns:
        True if webhook call succeeded, False otherwise
    """
    if not url:
        return False

    auth = HTTPBasicAuth(username, password) if username and password else None

    def make_request() -> requests.Response:
        return requests.request(
            method=method,
            url=url,
            auth=auth,
            timeout=timeout,
            headers={"User-Agent": "external-dns/1.0"},
        )

    try:
        response = retry_with_backoff(
            make_request,
            max_retries=max_retries,
            base_delay=1.0,
            max_delay=10.0,
        )
        if response.ok:
            logger.info(f"Webhook called successfully: {method} {url} -> {response.status_code}")
            return True
        else:
            logger.warning(
                f"Webhook returned non-success status: {method} {url} -> {response.status_code}"
            )
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook call failed after retries: {method} {url} -> {e}")
        return False


@dataclass
class RecordTargetConfig:
    """Configuration for a DNS or non-DNS provider."""

    name: str
    provider: str  # adguard, cloudflare, etc.
    url: str
    username: str = ""
    password: str = ""
    api_token: str = ""  # For providers that use API tokens
    zones: List[str] = field(default_factory=list)


# Compatibility name retained for external callers.
DNSProviderConfig = RecordTargetConfig


def _parse_dns_zones(value: Any) -> List[str]:
    """Parse DNS zone config from a YAML list or comma-separated string."""
    if isinstance(value, str):
        raw_zones = value.split(",")
    elif isinstance(value, list):
        raw_zones = value
    else:
        raw_zones = []

    zones = []
    for zone in raw_zones:
        normalized = str(zone or "").strip().rstrip(".").lower()
        if normalized:
            zones.append(normalized)
    return zones


def load_dns_providers_from_yaml(config_path: str) -> List[RecordTargetConfig]:
    """Load provider configurations from YAML config file.

    Supports two formats:
    - New format: 'providers' list with 'provider' field for type
    - Legacy format: 'dns_provider' single dict (backwards compatible)

    Args:
        config_path: Path to config file or directory

    Returns:
        List of RecordTargetConfig objects
    """
    config_files = find_config_files(config_path)
    if not config_files:
        return []

    providers: List[RecordTargetConfig] = []

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
                        providers.append(
                            RecordTargetConfig(
                                name=name,
                                provider=provider_type,
                                url=url,
                                username=str(item.get("username") or "").strip(),
                                password=str(item.get("password") or "").strip(),
                                api_token=str(item.get("api_token") or "").strip(),
                                zones=_parse_dns_zones(item.get("zones")),
                            )
                        )

            # Legacy format: 'dns_provider' single dict
            elif "dns_provider" in config_data:
                dns_config = config_data["dns_provider"]
                if isinstance(dns_config, dict):
                    url = str(dns_config.get("url") or "").strip()
                    if url:
                        providers.append(
                            RecordTargetConfig(
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


def load_dns_config_from_yaml(config_path: str) -> Optional[Dict[str, Any]]:
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
        "api_token": p.api_token,
        "zones": p.zones,
        "provider": p.provider,
    }


@dataclass
class WebhookConfig:
    """Configuration for post-sync webhook notifications."""

    url: str = ""
    username: str = ""
    password: str = ""
    method: str = "POST"
    timeout: int = 30
    only_on_changes: bool = True

    @property
    def enabled(self) -> bool:
        """Check if webhook is configured and enabled."""
        return bool(self.url)


@dataclass
class RuntimeSettings:
    """Runtime configuration settings."""

    sync_mode: str = "watch"
    poll_interval: int = 60
    log_level: str = "INFO"
    default_zone: str = "internal"
    takeover_existing_records: bool = False
    exclude_domains: List[str] = None  # type: ignore
    static_rewrites: Dict[str, str] = None  # type: ignore
    webhook: WebhookConfig = None  # type: ignore

    def __post_init__(self):
        if self.exclude_domains is None:
            self.exclude_domains = []
        if self.static_rewrites is None:
            self.static_rewrites = {}
        if self.webhook is None:
            self.webhook = WebhookConfig()


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
                if "takeover_existing_records" in s:
                    settings.takeover_existing_records = _parse_bool(
                        s["takeover_existing_records"], default=False
                    )

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

            # Load webhook configuration
            if "webhook" in config_data and isinstance(config_data["webhook"], dict):
                w = config_data["webhook"]
                settings.webhook = WebhookConfig(
                    url=str(w.get("url") or "").strip(),
                    username=str(w.get("username") or "").strip(),
                    password=str(w.get("password") or "").strip(),
                    method=str(w.get("method") or "POST").strip().upper(),
                    timeout=int(w.get("timeout", 30)),
                    only_on_changes=bool(w.get("only_on_changes", True)),
                )

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
    if os.getenv("EXTERNAL_DNS_TAKEOVER_EXISTING_RECORDS"):
        settings.takeover_existing_records = _parse_bool(
            os.getenv("EXTERNAL_DNS_TAKEOVER_EXISTING_RECORDS"),
            default=False,
        )

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

    # Webhook env var overrides (EXTERNAL_DNS_WEBHOOK_*)
    if os.getenv("EXTERNAL_DNS_WEBHOOK_URL"):
        settings.webhook.url = os.getenv("EXTERNAL_DNS_WEBHOOK_URL", "").strip()
    if os.getenv("EXTERNAL_DNS_WEBHOOK_USERNAME"):
        settings.webhook.username = os.getenv("EXTERNAL_DNS_WEBHOOK_USERNAME", "").strip()
    if os.getenv("EXTERNAL_DNS_WEBHOOK_PASSWORD"):
        settings.webhook.password = os.getenv("EXTERNAL_DNS_WEBHOOK_PASSWORD", "").strip()
    if os.getenv("EXTERNAL_DNS_WEBHOOK_METHOD"):
        settings.webhook.method = os.getenv("EXTERNAL_DNS_WEBHOOK_METHOD", "POST").strip().upper()
    if os.getenv("EXTERNAL_DNS_WEBHOOK_TIMEOUT"):
        settings.webhook.timeout = int(os.getenv("EXTERNAL_DNS_WEBHOOK_TIMEOUT", "30"))
    if os.getenv("EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES"):
        val = os.getenv("EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES", "true").strip().lower()
        settings.webhook.only_on_changes = val in ("true", "1", "yes")

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

# Technitium configuration (env vars as fallback, YAML config takes priority)
TECHNITIUM_URL = os.getenv("TECHNITIUM_URL", "")
TECHNITIUM_API_TOKEN = os.getenv("TECHNITIUM_API_TOKEN", "")
TECHNITIUM_ZONES = os.getenv("TECHNITIUM_ZONES", "")

# Goku golinks configuration (env vars as fallback, YAML config takes priority)
GOKU_URL = os.getenv("GOKU_URL", "")
GOKU_API_TOKEN = os.getenv("GOKU_API_TOKEN", "")

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


@dataclass(frozen=True, init=False)
class ManagedRecord:
    """A provider-neutral key/value record.

    DNS providers interpret these fields as domain/answer. Golink providers interpret
    them as alias/destination. The compatibility properties keep the existing
    public DNSRecord API working while reconciliation uses neutral terminology.
    """

    key: str
    value: str

    def __init__(
        self,
        key: Optional[str] = None,
        value: Optional[str] = None,
        *,
        domain: Optional[str] = None,
        answer: Optional[str] = None,
    ) -> None:
        if key is not None and domain is not None:
            raise TypeError("Pass key or domain, not both")
        if value is not None and answer is not None:
            raise TypeError("Pass value or answer, not both")
        resolved_key = key if key is not None else domain
        resolved_value = value if value is not None else answer
        if resolved_key is None or resolved_value is None:
            raise TypeError("ManagedRecord requires a key/value pair")
        object.__setattr__(self, "key", resolved_key)
        object.__setattr__(self, "value", resolved_value)

    @property
    def domain(self) -> str:
        """Compatibility alias for DNS-specific callers."""
        return self.key

    @property
    def answer(self) -> str:
        """Compatibility alias for DNS-specific callers."""
        return self.value


# Backwards-compatible public name. New generic code should use ManagedRecord.
DNSRecord = ManagedRecord


@dataclass(frozen=True)
class ProxyRoute:
    """Represents a route discovered from a reverse proxy."""

    hostname: str
    source_name: str
    target_ip: str
    zone: DNSZone = DNSZone.INTERNAL
    router_name: str = ""
    publish_external: bool = False
    golink_alias: str = ""
    golink_aliases: List[str] = field(default_factory=list)
    golink_destination: str = ""
    golink_enabled: bool = True


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
    public_target_ip: str = ""
    router_filter: str = ""
    middleware_filter: str = ""
    golink_alias_template: str = "{app}"
    golink_exclude_middlewares: List[str] = field(default_factory=lambda: ["no-golink"])


# =============================================================================
# Provider Interface and Implementations
# =============================================================================


class RecordTarget(ABC):
    """Abstract target that reconciles provider-specific key/value records."""

    applies_domain_exclusions = True
    record_kind = "DNS record"
    record_collection = "DNS records"
    key_name = "domain"
    value_name = "answer"
    conflicts_fail_closed = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name for logging."""
        pass

    @property
    def configured_name(self) -> str:
        """Return the operator-assigned target name when available."""
        return str(getattr(self, "_config_name", "") or self.name)

    @abstractmethod
    def test_connection(self) -> bool:
        """Test connection to the target."""
        pass

    @abstractmethod
    def get_records(self) -> List[ManagedRecord]:
        """Get all records visible to this target."""
        pass

    @abstractmethod
    def add_record(self, key: str, value: str) -> bool:
        """Add a provider-specific record."""
        pass

    @abstractmethod
    def delete_record(self, key: str, value: str) -> bool:
        """Delete a provider-specific record."""
        pass

    def update_record(self, key: str, old_value: str, new_value: str) -> bool:
        """Update an existing record. Default implementation: delete + add."""
        if self.delete_record(key, old_value):
            return self.add_record(key, new_value)
        return False

    def desired_static_record(self, domain: str, answer: str) -> Optional[ManagedRecord]:
        """Project a static rewrite into this provider's record shape."""
        if not domain or not answer:
            return None
        return ManagedRecord(key=domain, value=answer)

    def desired_source_record(
        self, hostname: str, source: Dict[str, Any]
    ) -> Optional[ManagedRecord]:
        """Project a discovered route source into this provider's record shape."""
        answer = str(source.get("answer") or "").strip()
        if not hostname or not answer:
            return None
        return ManagedRecord(key=hostname, value=answer)

    def desired_source_records(self, hostname: str, source: Dict[str, Any]) -> List[ManagedRecord]:
        """Project a discovered route source into this provider's record shape."""
        record = self.desired_source_record(hostname, source)
        return [record] if record is not None else []

    def choose_conflicting_record(
        self,
        record_name: str,
        candidates: List[tuple[str, str, str]],
    ) -> Optional[tuple[str, str, str]]:
        """Choose an unambiguous candidate, or return None to fail the conflict closed."""
        return None

    def conflict_hint(self) -> str:
        return "make the record key unique at the source"


class RecordTargetReadError(Exception):
    """Raised when target records cannot be read safely."""


# Compatibility aliases for callers importing the historical DNS-only API.
DNSProvider = RecordTarget
DNSProviderReadError = RecordTargetReadError


class AdGuardDNSProvider(RecordTarget):
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
            logger.error(
                f"[{self.configured_name}] Cannot connect to {self.name} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
            return False

    def get_records(self) -> List[DNSRecord]:
        def _do_request() -> Any:
            response = self._session.get(f"{self._url}/control/rewrite/list", timeout=5)
            response.raise_for_status()
            return response.json()

        try:
            data = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            raise RecordTargetReadError(_concise_error(e)) from e

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
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[{self.configured_name}] Failed to add DNS record {domain} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
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
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[{self.configured_name}] Failed to delete DNS record {domain} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
            return False


class TechnitiumAPIError(Exception):
    """Recoverable Technitium API status error."""


class TechnitiumDNSProvider(RecordTarget):
    """Technitium DNS Server provider implementation."""

    def __init__(self, url: str, api_token: str, zones: List[str]):
        self._url = url.rstrip("/")
        self._zones = _parse_dns_zones(zones)
        self._session = requests.Session()
        if api_token:
            self._session.headers.update({"Authorization": f"Bearer {api_token}"})

    @property
    def name(self) -> str:
        return "Technitium DNS Server"

    def _api_get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Call a Technitium JSON-status API endpoint."""

        def _do_request() -> Dict[str, Any]:
            if params is None:
                response = self._session.get(f"{self._url}{path}", timeout=5)
            else:
                response = self._session.get(f"{self._url}{path}", params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise TechnitiumAPIError("malformed response")
            status = data.get("status")
            if status != "ok":
                message = (
                    data.get("errorMessage") if isinstance(data.get("errorMessage"), str) else ""
                )
                detail = f"{status}: {message}" if message else str(status or "missing status")
                raise TechnitiumAPIError(detail)
            return data

        return retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)

    def _domain_matches_zone(self, domain: str, zone: str) -> bool:
        domain_l = domain.rstrip(".").lower()
        zone_l = zone.rstrip(".").lower()
        return domain_l == zone_l or domain_l.endswith(f".{zone_l}")

    def _zone_for_domain(self, domain: str) -> Optional[str]:
        matches = [zone for zone in self._zones if self._domain_matches_zone(domain, zone)]
        return max(matches, key=len) if matches else None

    def _write_a_record(self, path: str, domain: str, params: Dict[str, str]) -> bool:
        domain_l = domain.rstrip(".").lower()
        zone = self._zone_for_domain(domain_l)
        if not zone:
            logger.warning(f"No configured Technitium zone matches {domain_l}")
            return False

        request_params = {
            "domain": domain_l,
            "zone": zone,
            "type": "A",
            **params,
        }
        try:
            self._api_get(path, params=request_params)
            return True
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
            TechnitiumAPIError,
        ) as e:
            logger.error(
                f"[{self.configured_name}] Technitium A-record write failed for {domain_l} "
                f"at {self._url}: {_endpoint_error(self._url, e)}"
            )
            return False

    def test_connection(self) -> bool:
        try:
            self._api_get("/api/status")
            logger.info(f"{self.name} connection successful")
            return True
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
            TechnitiumAPIError,
        ) as e:
            logger.error(
                f"[{self.configured_name}] Cannot connect to {self.name} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
            return False

    def get_records(self) -> List[DNSRecord]:
        records: List[DNSRecord] = []
        try:
            for zone in self._zones:
                data = self._api_get(
                    "/api/zones/records/get",
                    params={"domain": zone, "zone": zone, "listZone": "true"},
                )
                response = data.get("response")
                zone_records = response.get("records") if isinstance(response, dict) else None
                if not isinstance(zone_records, list):
                    logger.warning(f"Skipping malformed Technitium zone response for {zone}")
                    continue

                for record in zone_records:
                    if not isinstance(record, dict):
                        logger.warning(f"Skipping malformed Technitium record in {zone}: {record}")
                        continue
                    if record.get("disabled") is True or record.get("type") != "A":
                        continue
                    domain = record.get("name")
                    rdata = record.get("rData")
                    answer = rdata.get("ipAddress") if isinstance(rdata, dict) else None
                    if not isinstance(domain, str) or not isinstance(answer, str):
                        logger.warning(
                            f"Skipping malformed Technitium A record in {zone}: {record}"
                        )
                        continue
                    if not self._domain_matches_zone(domain, zone):
                        logger.warning(
                            f"Skipping Technitium record outside configured zone {zone}: {domain}"
                        )
                        continue
                    records.append(
                        DNSRecord(domain=domain.rstrip(".").lower(), answer=answer.strip())
                    )
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
            TechnitiumAPIError,
        ) as e:
            raise RecordTargetReadError(_concise_error(e)) from e

        return records

    def add_record(self, domain: str, answer: str) -> bool:
        if not domain or not answer:
            return False
        result = self._write_a_record(
            "/api/zones/records/add",
            domain,
            {"ipAddress": answer.strip()},
        )
        return result

    def delete_record(self, domain: str, answer: str) -> bool:
        if not domain or not answer:
            return False
        result = self._write_a_record(
            "/api/zones/records/delete",
            domain,
            {"ipAddress": answer.strip()},
        )
        return result

    def update_record(self, domain: str, old_answer: str, new_answer: str) -> bool:
        if not domain or not old_answer or not new_answer:
            return False
        result = self._write_a_record(
            "/api/zones/records/update",
            domain,
            {
                "ipAddress": old_answer.strip(),
                "newIpAddress": new_answer.strip(),
            },
        )
        return result


class GokuProvider(RecordTarget):
    """Goku golinks provider."""

    applies_domain_exclusions = False
    record_kind = "golink alias"
    record_collection = "golink aliases"
    key_name = "alias"
    value_name = "destination"
    conflicts_fail_closed = True

    def __init__(self, url: str, api_token: str = "", username: str = "", password: str = ""):
        self._url = url.rstrip("/")
        self._session = requests.Session()
        if api_token:
            self._session.headers.update({"Authorization": f"Bearer {api_token}"})
        if username and password:
            self._session.auth = HTTPBasicAuth(username, password)

    @property
    def name(self) -> str:
        return "Goku golinks"

    def _post_form(self, path: str, data: Dict[str, str]) -> bool:
        def _do_request() -> bool:
            response = self._session.post(
                f"{self._url}{path}",
                data=data,
                timeout=5,
                allow_redirects=False,
            )
            if response.status_code not in range(200, 400):
                response.raise_for_status()
            return True

        retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
        return True

    def test_connection(self) -> bool:
        def _do_request() -> bool:
            response = self._session.get(f"{self._url}/api/aliases", timeout=5)
            response.raise_for_status()
            return True

        try:
            retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
            logger.info(f"{self.name} connection successful")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[{self.configured_name}] Cannot connect to {self.name} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
            return False

    def get_records(self) -> List[DNSRecord]:
        def _do_request() -> Any:
            response = self._session.get(f"{self._url}/api/aliases", timeout=5)
            response.raise_for_status()
            return response.json()

        try:
            data = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            raise RecordTargetReadError(_concise_error(e)) from e

        if not isinstance(data, list):
            raise RecordTargetReadError(
                f"invalid aliases response: expected a list, got {type(data).__name__}"
            )

        records: List[DNSRecord] = []
        for item in data:
            alias = item.get("alias") if isinstance(item, dict) else None
            destination = item.get("destination") if isinstance(item, dict) else None
            if not isinstance(alias, str) or not isinstance(destination, str):
                logger.warning(f"Skipping malformed Goku alias: {item}")
                continue
            alias = alias.strip().strip("/")
            destination = destination.strip()
            if alias and destination:
                records.append(DNSRecord(domain=alias, answer=destination))
        return records

    def add_record(self, domain: str, answer: str) -> bool:
        alias = domain.strip().strip("/")
        destination = answer.strip()
        if not alias or not destination:
            return False
        try:
            self._post_form("/api/aliases", {"alias": alias, "destination": destination})
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[{self.configured_name}] Failed to add golink alias {alias} at {self._url}: "
                f"{_endpoint_error(self._url, e)}"
            )
            return False

    def delete_record(self, domain: str, answer: str) -> bool:
        alias = domain.strip().strip("/")
        if not alias:
            return False
        try:
            self._post_form("/api/aliases/delete", {"alias": alias})
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                f"[{self.configured_name}] Failed to delete golink alias {alias} at "
                f"{self._url}: {_endpoint_error(self._url, e)}"
            )
            return False

    def update_record(self, domain: str, old_answer: str, new_answer: str) -> bool:
        return self.add_record(domain, new_answer)

    def desired_static_record(self, domain: str, answer: str) -> Optional[DNSRecord]:
        return None

    def desired_source_record(self, hostname: str, source: Dict[str, Any]) -> Optional[DNSRecord]:
        if not _parse_bool(source.get("golink_enabled"), default=True):
            return None
        alias = str(source.get("golink_alias") or "").strip().strip("/")
        destination = str(source.get("golink_destination") or "").strip()
        if not alias or not destination:
            return None
        return DNSRecord(domain=alias, answer=destination)

    def desired_source_records(self, hostname: str, source: Dict[str, Any]) -> List[DNSRecord]:
        if not _parse_bool(source.get("golink_enabled"), default=True):
            return []
        destination = str(source.get("golink_destination") or "").strip()
        if not destination:
            return []
        raw_aliases = source.get("golink_aliases")
        if isinstance(raw_aliases, list):
            aliases = [_slugify_golink_alias(str(alias)) for alias in raw_aliases]
        else:
            aliases = [_slugify_golink_alias(str(source.get("golink_alias") or ""))]

        records: List[DNSRecord] = []
        seen: Set[str] = set()
        for alias in aliases:
            if not alias or alias in seen:
                continue
            seen.add(alias)
            records.append(DNSRecord(domain=alias, answer=destination))
        return records

    def choose_conflicting_record(
        self,
        record_name: str,
        candidates: List[tuple[str, str, str]],
    ) -> Optional[tuple[str, str, str]]:
        """Prefer the one destination whose hostname basename is the alias itself."""
        alias = _slugify_golink_alias(record_name)
        exact_matches = [
            candidate
            for candidate in candidates
            if _slugify_golink_alias(candidate[2].rstrip(".").split(".", 1)[0]) == alias
        ]
        exact_answers = {answer for _, answer, _ in exact_matches}
        if len(exact_matches) == 1 or len(exact_answers) == 1:
            return exact_matches[0]
        return None

    def conflict_hint(self) -> str:
        return (
            "set sources[].golink_alias_template (for example '{app}-{source}') "
            "or an explicit external-dns.golink.alias label"
        )


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


class RouteSourceReadError(Exception):
    """Raised when a route source responds but cannot be read safely."""


class TraefikProxyProvider(ReverseProxyProvider):
    """Traefik reverse proxy provider implementation."""

    HOST_CALL_RE = re.compile(r"Host\(([^)]*)\)")
    HOST_ARG_RE = re.compile(r"[`\"\']([^`\"\']+)[`\"\']")
    HOSTNAME_RE = re.compile(
        r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$",
        re.IGNORECASE,
    )
    ZONE_SUFFIX_RE = re.compile(r"-(internal|external)(?:@|$)", re.IGNORECASE)
    ROUTER_ENTRYPOINT_PREFIX_RE = re.compile(
        r"^(?:websecure|web|https|http|tcp|udp|ws|wss)-",
        re.IGNORECASE,
    )
    DOCKER_SERVICE_LABELS = (
        "com.docker.compose.service",
        "com.docker.swarm.service.name",
    )
    DOCKER_STACK_LABELS = (
        "com.docker.compose.project",
        "com.docker.stack.namespace",
    )
    GOLINK_ALIAS_LABELS = (
        "external-dns.golink.alias",
        "external-dns.golinks.alias",
    )
    GOLINK_ENABLED_LABELS = (
        "external-dns.golink.enabled",
        "external-dns.golinks.enabled",
    )
    GOLINK_DESTINATION_LABELS = (
        "external-dns.golink.destination",
        "external-dns.golinks.destination",
    )

    def __init__(
        self,
        config_path: str = "",
        instances_json: str = "",
        url: str = "",
        target_ip: str = "",
        timeout_seconds: float = 5.0,
        default_zone: str = "internal",
    ):
        self._config_path = config_path
        self._instances_json = instances_json
        self._url = url
        self._target_ip = target_ip
        self._timeout = timeout_seconds
        self._default_zone = DNSZone.INTERNAL if default_zone != "external" else DNSZone.EXTERNAL

    @property
    def name(self) -> str:
        return "Traefik"

    def get_instances(self) -> List[ProxyInstance]:
        # Try loading from YAML config file(s) first
        if self._config_path:
            config_files = find_config_files(self._config_path)
            if config_files:
                all_instances: List[ProxyInstance] = []
                yaml_sources_configured = False

                for config_file in config_files:
                    try:
                        with open(config_file, "r") as f:
                            config_data = yaml.safe_load(f)

                        if not config_data or "sources" not in config_data:
                            logger.warning(f"Config file {config_file} missing 'sources' key")
                            continue

                        yaml_sources_configured = True
                        for item in config_data["sources"]:
                            if not isinstance(item, dict):
                                continue
                            name = str(item.get("name") or "traefik").strip()
                            url = str(item.get("url") or "").strip()
                            target_ip = str(
                                item.get("target_ip") or item.get("internal_ip") or ""
                            ).strip()
                            if not url or not target_ip:
                                logger.error(
                                    f"Invalid Traefik source '{name}' in {config_file}: "
                                    "url and target_ip are required"
                                )
                                continue
                            instance_type = str(item.get("type") or "traefik").strip()
                            verify_tls = _parse_bool(item.get("verify_tls"), default=True)
                            username = str(item.get("username") or "").strip()
                            password = str(item.get("password") or "").strip()
                            public_target_ip = str(item.get("public_target_ip") or "").strip()
                            router_filter = str(item.get("router_filter") or "").strip()
                            middleware_filter = str(item.get("middleware_filter") or "").strip()
                            golink_alias_template = str(
                                item.get("golink_alias_template") or "{app}"
                            ).strip()
                            golink_exclude_middlewares = _parse_string_list(
                                item.get("golink_exclude_middlewares"),
                                default=["no-golink"],
                            )
                            all_instances.append(
                                ProxyInstance(
                                    name=name,
                                    url=url,
                                    target_ip=target_ip,
                                    type=instance_type,
                                    verify_tls=verify_tls,
                                    username=username,
                                    password=password,
                                    public_target_ip=public_target_ip,
                                    router_filter=router_filter,
                                    middleware_filter=middleware_filter,
                                    golink_alias_template=golink_alias_template or "{app}",
                                    golink_exclude_middlewares=golink_exclude_middlewares,
                                )
                            )
                    except Exception as e:
                        logger.error(f"Failed to load config from {config_file}: {e}")

                if all_instances:
                    logger.debug(
                        f"Loaded {len(all_instances)} Traefik source(s) from "
                        f"{len(config_files)} config file(s)"
                    )
                    return all_instances
                if yaml_sources_configured:
                    return []

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
                    public_target_ip = str(item.get("public_target_ip") or "").strip()
                    router_filter = str(item.get("router_filter") or "").strip()
                    middleware_filter = str(item.get("middleware_filter") or "").strip()
                    golink_alias_template = str(
                        item.get("golink_alias_template") or "{app}"
                    ).strip()
                    golink_exclude_middlewares = _parse_string_list(
                        item.get("golink_exclude_middlewares"),
                        default=["no-golink"],
                    )
                    instances.append(
                        ProxyInstance(
                            name=name,
                            url=url,
                            target_ip=target_ip,
                            type=instance_type,
                            verify_tls=verify_tls,
                            username=username,
                            password=password,
                            public_target_ip=public_target_ip,
                            router_filter=router_filter,
                            middleware_filter=middleware_filter,
                            golink_alias_template=golink_alias_template or "{app}",
                            golink_exclude_middlewares=golink_exclude_middlewares,
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

        routers = retry_with_backoff(_do_request, max_retries=2, base_delay=1.0)

        # Validate routers is a list
        if not isinstance(routers, list):
            raise RouteSourceReadError(
                f"invalid routers response: expected a list, got {type(routers).__name__}"
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
            publish_external = zone == DNSZone.EXTERNAL and bool(instance.public_target_ip)
            route_target_ip = instance.public_target_ip if publish_external else instance.target_ip
            labels = self._router_labels(router)
            app_slug = self._router_app_slug(router_name, router, labels, instance)
            golink_enabled = self._golink_enabled(router, labels, instance)
            explicit_golink_alias = self._first_label_value(labels, self.GOLINK_ALIAS_LABELS)
            explicit_golink_destination = self._first_label_value(
                labels, self.GOLINK_DESTINATION_LABELS
            )
            route_specs: List[Dict[str, Any]] = []

            for hostname in self._extract_hostnames(rule):
                golink_aliases = self._golink_aliases(
                    hostname=hostname,
                    router_name=router_name,
                    instance=instance,
                    app_slug=app_slug,
                    explicit_alias=explicit_golink_alias,
                )
                golink_destination = (
                    explicit_golink_destination.strip()
                    if explicit_golink_destination
                    else f"https://{hostname.rstrip('.')}"
                )
                route_specs.append(
                    {
                        "hostname": hostname,
                        "golink_aliases": golink_aliases,
                        "golink_destination": golink_destination,
                        "golink_enabled": golink_enabled,
                    }
                )
            for spec in route_specs:
                routes.append(
                    ProxyRoute(
                        hostname=str(spec["hostname"]),
                        source_name=instance.name,
                        target_ip=route_target_ip,
                        zone=zone,
                        router_name=router_name,
                        publish_external=publish_external,
                        golink_alias=(
                            str(spec["golink_aliases"][0]) if spec["golink_aliases"] else ""
                        ),
                        golink_aliases=list(spec["golink_aliases"]),
                        golink_destination=str(spec["golink_destination"]),
                        golink_enabled=bool(spec["golink_enabled"]),
                    )
                )
        return self._disable_duplicate_instance_golinks(routes)

    def _router_labels(self, router: Dict[str, Any]) -> Dict[str, str]:
        """Return router labels if the proxy API exposes them."""
        labels: Dict[str, str] = {}
        for key in ("labels", "Labels", "providerLabels", "dockerLabels"):
            raw_labels = router.get(key)
            if isinstance(raw_labels, dict):
                for raw_key, raw_value in raw_labels.items():
                    labels[str(raw_key).strip().lower()] = str(raw_value).strip()
            elif isinstance(raw_labels, list):
                for item in raw_labels:
                    if not isinstance(item, str) or "=" not in item:
                        continue
                    raw_key, raw_value = item.split("=", 1)
                    labels[raw_key.strip().lower()] = raw_value.strip()
        return labels

    def _first_label_value(self, labels: Dict[str, str], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = labels.get(key.lower(), "").strip()
            if value:
                return value
        return ""

    def _router_app_slug(
        self,
        router_name: str,
        router: Dict[str, Any],
        labels: Optional[Dict[str, str]] = None,
        instance: Optional[ProxyInstance] = None,
    ) -> str:
        label_map = labels or self._router_labels(router)
        suffixes = self._stack_suffixes(label_map, instance)
        candidates = [
            self._first_label_value(label_map, self.DOCKER_SERVICE_LABELS),
            str(router.get("service") or ""),
            router_name,
        ]
        for candidate in candidates:
            base = candidate.split("@", 1)[0].strip()
            base = self.ZONE_SUFFIX_RE.sub("", base)
            if candidate == router_name:
                base = self.ROUTER_ENTRYPOINT_PREFIX_RE.sub("", base)
            base = self._normalize_service_slug(base, suffixes)
            slug = _slugify_golink_alias(base)
            if slug:
                return slug
        return ""

    def _stack_suffixes(
        self, labels: Dict[str, str], instance: Optional[ProxyInstance]
    ) -> Set[str]:
        suffixes: Set[str] = set()
        if instance:
            source = _slugify_golink_alias(instance.name)
            if source:
                suffixes.add(source)
        for key in self.DOCKER_STACK_LABELS:
            value = _slugify_golink_alias(labels.get(key, ""))
            if value:
                suffixes.add(value)
        return suffixes

    def _normalize_service_slug(self, value: str, suffixes: Set[str]) -> str:
        slug = _slugify_golink_alias(value)
        if not slug:
            return ""
        slug = re.sub(r"^\d+-", "", slug)
        slug = self._strip_known_suffix(slug, suffixes)
        slug = re.sub(r"-service$", "", slug)
        slug = self._collapse_truenas_ix_slug(slug)
        slug = self._strip_known_suffix(slug, suffixes)
        return slug

    def _collapse_truenas_ix_slug(self, slug: str) -> str:
        """Collapse TrueNAS ix generated service names like app-ix-app."""
        parts = slug.split("-ix-")
        if len(parts) == 2 and parts[0] and parts[0] == parts[1]:
            return parts[0]
        return slug

    def _strip_known_suffix(self, slug: str, suffixes: Set[str]) -> str:
        for suffix in sorted(suffixes, key=len, reverse=True):
            if suffix and slug.endswith(f"-{suffix}"):
                return slug[: -(len(suffix) + 1)]
        return slug

    def _disable_duplicate_instance_golinks(self, routes: List[ProxyRoute]) -> List[ProxyRoute]:
        """Keep one GoLink destination per alias from one Traefik source."""
        by_alias: Dict[str, List[ProxyRoute]] = {}
        for route in routes:
            if not route.golink_enabled:
                continue
            aliases = route.golink_aliases or ([route.golink_alias] if route.golink_alias else [])
            for alias in aliases:
                if alias and route.golink_destination:
                    by_alias.setdefault(alias, []).append(route)

        aliases_by_route_id: Dict[int, List[str]] = {}
        for route in routes:
            aliases_by_route_id[id(route)] = list(
                route.golink_aliases or ([route.golink_alias] if route.golink_alias else [])
            )

        for alias, specs in by_alias.items():
            destinations = {route.golink_destination for route in specs}
            if len(destinations) <= 1:
                continue
            keep = min(
                specs,
                key=lambda route: self._golink_hostname_rank(route.hostname, alias),
            )
            for spec in specs:
                if spec is not keep:
                    aliases_by_route_id[id(spec)] = [
                        item for item in aliases_by_route_id[id(spec)] if item != alias
                    ]

        if all(
            aliases_by_route_id[id(route)]
            == list(route.golink_aliases or ([route.golink_alias] if route.golink_alias else []))
            for route in routes
        ):
            return routes
        updated_routes: List[ProxyRoute] = []
        for route in routes:
            aliases = aliases_by_route_id[id(route)]
            updated_routes.append(
                replace(
                    route,
                    golink_alias=aliases[0] if aliases else "",
                    golink_aliases=aliases,
                    golink_enabled=route.golink_enabled and bool(aliases),
                )
            )
        return updated_routes

    def _golink_hostname_rank(self, hostname: str, app_slug: str) -> tuple[int, int, str]:
        first_label = _slugify_golink_alias(hostname.rstrip(".").split(".", 1)[0])
        app = _slugify_golink_alias(app_slug)
        exact_app_match = 0 if app and first_label == app else 1
        return (exact_app_match, hostname.count("."), hostname)

    def _golink_enabled(
        self, router: Dict[str, Any], labels: Dict[str, str], instance: ProxyInstance
    ) -> bool:
        enabled_label = self._first_label_value(labels, self.GOLINK_ENABLED_LABELS)
        if enabled_label:
            return _parse_bool(enabled_label, default=True)
        for middleware in instance.golink_exclude_middlewares:
            if middleware and self._has_middleware(router, middleware):
                return False
        return True

    def _golink_aliases(
        self,
        *,
        hostname: str,
        router_name: str,
        instance: ProxyInstance,
        app_slug: str,
        explicit_alias: str,
    ) -> List[str]:
        if explicit_alias:
            alias = _slugify_golink_alias(explicit_alias)
            return [alias] if alias else []
        fallback_app = app_slug or _slugify_golink_alias(hostname.split(".", 1)[0])
        hostname_alias = _slugify_golink_alias(hostname.rstrip(".").split(".", 1)[0])
        base_aliases = []
        for alias in (fallback_app, hostname_alias):
            alias = _slugify_golink_alias(alias)
            if alias and alias not in base_aliases:
                base_aliases.append(alias)

        template = instance.golink_alias_template or "{app}"
        aliases: List[str] = []
        for base_alias in base_aliases:
            try:
                raw_alias = template.format(
                    app=base_alias,
                    source=_slugify_golink_alias(instance.name),
                    hostname=hostname.rstrip(".").lower(),
                    router=router_name.split("@", 1)[0],
                )
            except (KeyError, ValueError) as e:
                logger.warning(
                    f"Invalid golink_alias_template '{template}' for source '{instance.name}': {e}"
                )
                raw_alias = base_alias
            alias = _slugify_golink_alias(raw_alias)
            if alias and alias not in aliases:
                aliases.append(alias)
        return aliases

    def _detect_zone(self, router_name: str, router: Dict[str, Any]) -> DNSZone:
        """Detect DNS zone from router name suffix or default zone.

        Priority:
          1. Router name suffix: -internal or -external
          2. Default zone
        """
        # Check router name suffix (e.g., "myapp-internal@docker")
        if router_name:
            match = self.ZONE_SUFFIX_RE.search(router_name)
            if match:
                zone_str = match.group(1).lower()
                return DNSZone.EXTERNAL if zone_str == "external" else DNSZone.INTERNAL

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
        hostnames = set()
        for call in self.HOST_CALL_RE.finditer(rule or ""):
            for match in self.HOST_ARG_RE.finditer(call.group(1)):
                hostname = match.group(1).strip().lower()
                if hostname and self.HOSTNAME_RE.match(hostname):
                    hostname = hostname.rstrip(".")
                    hostnames.add(hostname)
        return sorted(hostnames)


# =============================================================================
# Provider Registry
# =============================================================================


def get_dns_config() -> Dict[str, Any]:
    """Get DNS provider configuration from YAML config or env vars.

    Priority: YAML config > environment variables

    Returns:
        Dict with provider-specific DNS configuration.
    """
    # Try YAML config first
    yaml_config = load_dns_config_from_yaml(CONFIG_PATH)
    if yaml_config:
        return yaml_config

    # Fall back to environment variables
    if DNS_PROVIDER == "technitium":
        return {
            "provider": "technitium",
            "url": TECHNITIUM_URL,
            "username": "",
            "password": "",
            "api_token": TECHNITIUM_API_TOKEN,
            "zones": _parse_dns_zones(TECHNITIUM_ZONES),
        }
    if DNS_PROVIDER == "goku":
        return {
            "provider": "goku",
            "url": GOKU_URL,
            "username": "",
            "password": "",
            "api_token": GOKU_API_TOKEN,
            "zones": [],
        }

    return {
        "provider": DNS_PROVIDER,
        "url": ADGUARD_URL,
        "username": ADGUARD_USERNAME,
        "password": ADGUARD_PASSWORD,
        "api_token": "",
        "zones": [],
    }


def _dns_provider_config_from_env() -> RecordTargetConfig:
    """Build one provider config from legacy environment variables."""
    config = get_dns_config()
    provider_type = str(config.get("provider") or DNS_PROVIDER).lower().strip()
    return RecordTargetConfig(
        name=provider_type or "default",
        provider=provider_type,
        url=str(config.get("url") or "").strip(),
        username=str(config.get("username") or "").strip(),
        password=str(config.get("password") or "").strip(),
        api_token=str(config.get("api_token") or "").strip(),
        zones=_parse_dns_zones(config.get("zones")),
    )


def _create_dns_provider_from_config(config: RecordTargetConfig) -> RecordTarget:
    """Create a concrete provider from normalized config."""
    provider_type = config.provider.lower().strip()
    supported = ["adguard", "technitium", "goku"]

    if provider_type == "adguard":
        target: RecordTarget = AdGuardDNSProvider(config.url, config.username, config.password)
    elif provider_type == "technitium":
        target = TechnitiumDNSProvider(config.url, config.api_token, config.zones)
    elif provider_type == "goku":
        target = GokuProvider(config.url, config.api_token, config.username, config.password)
    else:
        raise ValueError(
            f"Unsupported provider type '{provider_type}' for provider '{config.name}'. "
            f"Supported providers: {', '.join(supported)}."
        )
    target._config_name = config.name  # type: ignore[attr-defined]
    target._provider_type = provider_type  # type: ignore[attr-defined]
    return target


def create_record_targets() -> List[RecordTarget]:
    """Create all configured DNS and non-DNS providers."""
    yaml_providers = load_dns_providers_from_yaml(CONFIG_PATH)
    provider_configs = yaml_providers if yaml_providers else [_dns_provider_config_from_env()]
    return [_create_dns_provider_from_config(config) for config in provider_configs]


def create_dns_providers() -> List[RecordTarget]:
    """Compatibility wrapper for the original DNS-only factory name."""
    return create_record_targets()


def create_dns_provider() -> RecordTarget:
    """Create the first configured provider (legacy function name)."""
    try:
        return create_record_targets()[0]
    except ValueError as e:
        supported = ["adguard", "technitium", "goku"]
        provider_type = str(get_dns_config().get("provider") or DNS_PROVIDER).lower().strip()
        raise ValueError(
            f"Unsupported DNS_PROVIDER: '{provider_type}'. "
            f"Supported providers: {', '.join(supported)}. "
            f"Check your DNS_PROVIDER environment variable."
        ) from e


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


def _parse_string_list(value: Any, *, default: Optional[List[str]] = None) -> List[str]:
    """Parse a YAML/env string list from list or comma-separated string input."""
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return list(default or [])
    parsed = [str(item).strip() for item in items if str(item).strip()]
    return parsed if parsed else list(default or [])


def _slugify_golink_alias(value: str) -> str:
    """Normalize a discovered app/router name into a Goku alias slug."""
    slug = str(value or "").strip().strip("/").lower()
    slug = re.sub(r"[^a-z0-9._/-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-._/")
    return slug


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
        record_target: Optional[RecordTarget] = None,
        record_targets: Optional[List[RecordTarget]] = None,
        dns_provider: Optional[DNSProvider] = None,
        dns_providers: Optional[List[DNSProvider]] = None,
        proxy_provider: ReverseProxyProvider,
        state_store: StateStore,
        static_rewrites: Dict[str, str],
        exclude_patterns: List[re.Pattern],
        takeover_existing_records: bool = False,
    ):
        if record_targets is not None:
            providers = list(record_targets)
        elif record_target is not None:
            providers = [record_target]
        elif dns_providers is not None:
            providers = list(dns_providers)
        elif dns_provider is not None:
            providers = [dns_provider]
        else:
            raise ValueError("At least one provider is required")
        if not providers:
            raise ValueError("At least one provider is required")

        self.record_targets = providers
        self.record_target = providers[0]
        # Compatibility attributes for integrations using the original DNS-only API.
        self.dns_providers = providers
        self.dns_provider = providers[0]
        self.proxy_provider = proxy_provider
        self.state_store = state_store
        self.static_rewrites = static_rewrites
        self.exclude_patterns = exclude_patterns
        self.takeover_existing_records = takeover_existing_records
        self._startup_cleanup_done = False

    def _route_source_state(self, route: ProxyRoute, seen_at: int) -> Dict[str, Any]:
        """Build the state payload stored for a discovered route source."""
        return {
            "answer": route.target_ip,
            "last_seen": seen_at,
            "router_name": route.router_name,
            "golink_alias": route.golink_alias,
            "golink_aliases": route.golink_aliases
            or ([route.golink_alias] if route.golink_alias else []),
            "golink_destination": route.golink_destination,
            "golink_enabled": route.golink_enabled,
        }

    def _compute_desired_records_for_provider(
        self,
        provider: RecordTarget,
        state: Dict[str, Any],
        instances: List[ProxyInstance],
    ) -> tuple[Dict[str, str], Set[str]]:
        """Compute desired records in the provider's own record shape."""
        desired: Dict[str, str] = {}
        protected_records: Set[str] = set()

        for domain, answer in self.static_rewrites.items():
            record = provider.desired_static_record(domain, answer)
            if record is None:
                continue
            desired[record.key] = record.value
            protected_records.add(record.key)

        candidates: Dict[str, List[tuple[str, str, str]]] = {}
        for hostname, domain_state in state.get("domains", {}).items():
            if not isinstance(domain_state, dict):
                continue
            sources = domain_state.get("sources", {})
            if not isinstance(sources, dict):
                continue
            for source_name, source in sources.items():
                if not isinstance(source, dict):
                    continue
                for record in provider.desired_source_records(str(hostname), source):
                    candidates.setdefault(record.key, []).append(
                        (str(source_name), record.value, str(hostname))
                    )

        configured_order = [i.name for i in instances]
        target_label = self._target_log_label(provider)
        for record_name, record_candidates in sorted(candidates.items()):
            distinct_answers = sorted({answer for _, answer, _ in record_candidates if answer})
            if not distinct_answers:
                continue

            if len(distinct_answers) > 1:
                chosen = provider.choose_conflicting_record(record_name, record_candidates)
                if chosen is not None:
                    chosen_source, chosen_answer, chosen_hostname = chosen
                    logger.debug(
                        f"[{target_label}] Resolved {provider.record_kind.lower()} "
                        f"'{record_name}' to {chosen_source}/{chosen_hostname} -> {chosen_answer}"
                    )
                    desired[record_name] = chosen_answer
                    continue

                if provider.conflicts_fail_closed:
                    details = sorted(
                        {
                            f"{source}/{hostname} -> {answer}"
                            for source, answer, hostname in record_candidates
                        }
                    )
                    logger.warning(
                        f"[{target_label}] Ambiguous {provider.record_kind.lower()} "
                        f"'{record_name}'; candidates: {details}. No record will be published; "
                        f"{provider.conflict_hint()}."
                    )
                    continue

                chosen_source = ""
                chosen_answer = ""
                for source_name in configured_order:
                    for candidate_source, candidate_answer, _ in record_candidates:
                        if candidate_source == source_name:
                            chosen_source = candidate_source
                            chosen_answer = candidate_answer
                            break
                    if chosen_answer:
                        break
                if not chosen_answer:
                    chosen_source, chosen_answer, _ = record_candidates[0]

                logger.warning(
                    f"Domain '{record_name}' present on multiple proxy instances with different "
                    f"target IPs {distinct_answers}; using '{chosen_answer}' from '{chosen_source}'"
                )
                desired[record_name] = chosen_answer
                continue

            desired[record_name] = distinct_answers[0]

        return desired, protected_records

    def _target_state_key(self, provider: RecordTarget) -> str:
        """Return a stable, non-secret target identity for managed state."""
        identity = getattr(provider, "_url", "") or getattr(provider, "url", "")
        if identity:
            return f"{provider.name}:{identity}"
        return provider.name

    def _provider_state_key(self, provider: DNSProvider) -> str:
        """Compatibility wrapper for the original DNS-only method name."""
        return self._target_state_key(provider)

    def _target_log_label(self, provider: RecordTarget) -> str:
        """Return the configured target name without exposing URLs or credentials."""
        return provider.configured_name

    def _log_target_read_failure(
        self,
        provider: RecordTarget,
        operation: str,
        error: Exception,
        *,
        plan: bool = False,
    ) -> None:
        """Log a target read failure once, at the orchestration boundary."""
        label = self._target_log_label(provider)
        url = str(getattr(provider, "_url", "") or "configured endpoint")
        detail = _concise_error(error)
        message = (
            f"[{label}] {provider.record_collection} unavailable from {provider.name} "
            f"at {url}: {detail}{_local_endpoint_hint(url, detail)}; {operation} skipped"
        )
        (logger.error if plan else logger.warning)(message)

    def _log_source_read_failure(
        self,
        instance: ProxyInstance,
        error: Exception,
        *,
        preserving_state: bool,
    ) -> None:
        """Log one concise source failure with its state-safety behavior."""
        detail = _concise_error(error)
        suffix = "; keeping last-known routes" if preserving_state else "; source omitted from plan"
        logger.warning(
            f"Source '{instance.name}' unavailable (Traefik at {instance.url}): "
            f"{detail}{_local_endpoint_hint(instance.url, detail)}{suffix}"
        )

    def _is_record_managed(
        self,
        state: Dict[str, Any],
        domain: str,
        answer: str,
        provider: Optional[RecordTarget] = None,
    ) -> bool:
        """Check if a target record is owned by external-dns."""
        dns_provider = provider or self.record_target
        managed_by_provider = state.get("managed_records_by_provider", {})
        if isinstance(managed_by_provider, dict) and managed_by_provider:
            provider_key = self._target_state_key(dns_provider)
            provider_managed = managed_by_provider.get(provider_key, {})
            if isinstance(provider_managed, dict):
                return answer in provider_managed.get(domain, [])
            return False

        managed = state.get("managed_records", {})
        return answer in managed.get(domain, [])

    def _migrate_legacy_managed_records(self, state: Dict[str, Any]) -> None:
        """Seed provider-scoped ownership from legacy single-provider state."""
        legacy_managed = state.get("managed_records", {})
        managed_by_provider = state.setdefault("managed_records_by_provider", {})
        if (
            not isinstance(legacy_managed, dict)
            or not isinstance(managed_by_provider, dict)
            or not self.record_targets
        ):
            return

        provider_key = self._target_state_key(self.record_targets[0])
        provider_managed = managed_by_provider.setdefault(provider_key, {})
        if not isinstance(provider_managed, dict):
            return

        for domain, answers in legacy_managed.items():
            if not isinstance(answers, list):
                continue
            domain_answers = provider_managed.setdefault(str(domain), [])
            if not isinstance(domain_answers, list):
                continue
            for answer in answers:
                normalized_answer = str(answer)
                if normalized_answer and normalized_answer not in domain_answers:
                    domain_answers.append(normalized_answer)

    def _mark_record_managed(
        self,
        state: Dict[str, Any],
        domain: str,
        answer: str,
        provider: Optional[RecordTarget] = None,
    ) -> None:
        """Track a target record as managed by external-dns."""
        dns_provider = provider or self.record_target
        managed_by_provider = state.setdefault("managed_records_by_provider", {})
        provider_key = self._target_state_key(dns_provider)
        provider_managed = managed_by_provider.setdefault(provider_key, {})
        provider_answers = provider_managed.setdefault(domain, [])
        if answer not in provider_answers:
            provider_answers.append(answer)

        legacy_managed = state.setdefault("managed_records", {})
        domain_answers = legacy_managed.setdefault(domain, [])
        if answer not in domain_answers:
            domain_answers.append(answer)

    def _unmark_record_managed(
        self,
        state: Dict[str, Any],
        domain: str,
        answer: str,
        provider: Optional[RecordTarget] = None,
    ) -> None:
        """Remove a target record from managed tracking."""
        dns_provider = provider or self.record_target
        managed_by_provider = state.get("managed_records_by_provider", {})
        if isinstance(managed_by_provider, dict):
            provider_key = self._target_state_key(dns_provider)
            provider_managed = managed_by_provider.get(provider_key, {})
            if isinstance(provider_managed, dict) and domain in provider_managed:
                if answer in provider_managed[domain]:
                    provider_managed[domain].remove(answer)
                if not provider_managed[domain]:
                    del provider_managed[domain]
            if isinstance(provider_managed, dict) and not provider_managed:
                managed_by_provider.pop(provider_key, None)

        answer_still_provider_managed = False
        if isinstance(managed_by_provider, dict):
            for provider_managed in managed_by_provider.values():
                if isinstance(provider_managed, dict) and answer in provider_managed.get(
                    domain, []
                ):
                    answer_still_provider_managed = True
                    break

        managed = state.get("managed_records", {})
        if not answer_still_provider_managed and domain in managed:
            if answer in managed[domain]:
                managed[domain].remove(answer)
            if not managed[domain]:
                del managed[domain]

    def _sync_static_rewrites_for_provider(
        self, state: Dict[str, Any], provider: RecordTarget
    ) -> int:
        """Sync static rewrites and return the number of changes made."""
        if not self.static_rewrites:
            return 0

        desired_static = {
            record.key: record.value
            for domain, answer in self.static_rewrites.items()
            if (record := provider.desired_static_record(domain, answer)) is not None
        }
        if not desired_static:
            return 0

        changes = 0
        try:
            current_records = {r.key: r.value for r in provider.get_records()}
        except Exception as e:
            self._log_target_read_failure(provider, "static rewrite reconciliation", e)
            return 0

        for domain, answer in desired_static.items():
            if domain in current_records:
                current_answer = current_records[domain]
                if current_answer == answer:
                    # Record already exists with correct answer - mark as managed
                    self._mark_record_managed(state, domain, answer, provider)
                elif self._is_record_managed(state, domain, current_answer, provider):
                    # Record is managed by us with wrong answer - update it
                    logger.info(
                        f"[{self._target_log_label(provider)}] Updating static rewrite "
                        f"{domain}: {current_answer} -> {answer}"
                    )
                    if provider.update_record(domain, current_answer, answer):
                        self._unmark_record_managed(state, domain, current_answer, provider)
                        self._mark_record_managed(state, domain, answer, provider)
                        changes += 1
                elif self.takeover_existing_records:
                    logger.info(
                        f"[{self._target_log_label(provider)}] Taking ownership of static "
                        f"rewrite {domain}: {current_answer} -> {answer}"
                    )
                    if provider.update_record(domain, current_answer, answer):
                        self._mark_record_managed(state, domain, answer, provider)
                        changes += 1
                else:
                    # Pre-existing record not managed by us - warn and skip
                    logger.warning(
                        f"[{self._target_log_label(provider)}] Static rewrite "
                        f"{domain} -> {answer} conflicts with pre-existing "
                        f"record {domain} -> {current_answer} (not managed by external-dns, skipping)"
                    )
            else:
                logger.info(
                    f"[{self._target_log_label(provider)}] Adding static rewrite "
                    f"{domain} -> {answer}"
                )
                if provider.add_record(domain, answer):
                    self._mark_record_managed(state, domain, answer, provider)
                    changes += 1

        return changes

    def _sync_static_rewrites(self, state: Dict[str, Any]) -> int:
        """Sync static rewrites across targets that support DNS records."""
        changes = 0
        for provider in self.record_targets:
            changes += self._sync_static_rewrites_for_provider(state, provider)
        return changes

    def _cleanup_removed_instances(
        self, state: Dict[str, Any], instances: List[ProxyInstance]
    ) -> int:
        """Remove stale source ownership; normal reconciliation removes its records."""
        configured_names = {i.name for i in instances}
        state_instances = state.get("instances", {})
        removed_instances = set(state_instances.keys()) - configured_names

        if not removed_instances:
            return 0

        logger.info(f"Detected removed sources: {', '.join(sorted(removed_instances))}")

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

        for domain in sorted(domains_to_cleanup):
            state["domains"].pop(domain, None)

        # Remove the instance entries from state
        for removed_name in removed_instances:
            state["instances"].pop(removed_name, None)
            logger.info(f"Cleaned up state for removed source: {removed_name}")

        return 0

    def _reconcile_record_target(
        self,
        provider: RecordTarget,
        state: Dict[str, Any],
        desired: Dict[str, str],
        domains_to_delete_from_state: List[str],
        protected_records: Optional[Set[str]] = None,
    ) -> int:
        """Reconcile desired records against one target's current records."""
        provider_key = self._target_log_label(provider)
        protected = protected_records or set()
        changes = 0
        try:
            all_records = provider.get_records()
        except Exception as e:
            self._log_target_read_failure(provider, "record reconciliation", e)
            return 0

        # Build a mapping of domain -> list of answers (to detect duplicates)
        records_by_key: Dict[str, List[str]] = {}
        for r in all_records:
            records_by_key.setdefault(r.key, []).append(r.value)

        # Clean up existing DNS records that match exclusion patterns (only managed records)
        if self.exclude_patterns and provider.applies_domain_exclusions:
            for domain, answers in list(records_by_key.items()):
                # Skip static rewrites
                if domain in protected:
                    continue
                if _is_domain_excluded(domain, self.exclude_patterns):
                    deleted_any = False
                    for answer in answers:
                        if self._is_record_managed(state, domain, answer, provider):
                            logger.info(
                                f"[{provider_key}] Removing excluded domain from DNS: "
                                f"{domain} -> {answer}"
                            )
                            if provider.delete_record(domain, answer):
                                self._unmark_record_managed(state, domain, answer, provider)
                                changes += 1
                                deleted_any = True
                        else:
                            logger.debug(
                                f"[{provider_key}] Skipping pre-existing excluded record: "
                                f"{domain} -> {answer}"
                            )
                    # Remove from records_by_key so we don't process it later
                    if deleted_any:
                        del records_by_key[domain]

        # Apply creates/updates, handling duplicates (respecting managed records).
        for domain, answer in sorted(desired.items()):
            existing_answers = records_by_key.get(domain, [])

            if not existing_answers:
                # No existing record - add it and mark as managed
                logger.info(
                    f"[{provider_key}] Adding {provider.record_kind.lower()} {domain} -> {answer}"
                )
                if provider.add_record(domain, answer):
                    self._mark_record_managed(state, domain, answer, provider)
                    changes += 1
            elif len(existing_answers) == 1 and existing_answers[0] == answer:
                # Exactly one record with correct answer - adopt it as managed
                self._mark_record_managed(state, domain, answer, provider)
            else:
                # Either wrong answer(s) or duplicates exist
                # Check which records we can manage
                managed_answers = [
                    a
                    for a in existing_answers
                    if self._is_record_managed(state, domain, a, provider)
                ]
                unmanaged_answers = [
                    a
                    for a in existing_answers
                    if not self._is_record_managed(state, domain, a, provider)
                ]

                if unmanaged_answers:
                    # There are pre-existing records we didn't create
                    if self.takeover_existing_records:
                        logger.info(
                            f"[{provider_key}] Taking ownership of pre-existing record(s) "
                            f"{domain}: {existing_answers} -> {answer}"
                        )
                        removed_all = True
                        for old_answer in existing_answers:
                            if provider.delete_record(domain, old_answer):
                                self._unmark_record_managed(state, domain, old_answer, provider)
                                changes += 1
                            else:
                                removed_all = False
                        if removed_all and provider.add_record(domain, answer):
                            self._mark_record_managed(state, domain, answer, provider)
                            changes += 1
                    elif answer in unmanaged_answers:
                        # Desired answer already exists as pre-existing - adopt it
                        logger.debug(
                            f"[{provider_key}] Adopting pre-existing record {domain} -> {answer}"
                        )
                        self._mark_record_managed(state, domain, answer, provider)
                        # Clean up any managed duplicates
                        for old_answer in managed_answers:
                            if old_answer != answer:
                                logger.info(
                                    f"[{provider_key}] Removing managed duplicate "
                                    f"{domain} -> {old_answer}"
                                )
                                if provider.delete_record(domain, old_answer):
                                    self._unmark_record_managed(state, domain, old_answer, provider)
                                    changes += 1
                    else:
                        # Pre-existing record(s) with different answer - warn and skip
                        logger.warning(
                            f"[{provider_key}] {provider.key_name.capitalize()} {domain} has "
                            f"pre-existing {provider.record_collection.lower()} "
                            f"{unmanaged_answers} (not managed by external-dns); "
                            f"skipping desired {answer}"
                        )
                        # Still clean up our managed records for this domain
                        for old_answer in managed_answers:
                            logger.info(
                                f"[{provider_key}] Removing obsolete managed record "
                                f"{domain} -> {old_answer}"
                            )
                            if provider.delete_record(domain, old_answer):
                                self._unmark_record_managed(state, domain, old_answer, provider)
                                changes += 1
                else:
                    # All records are managed by us - clean up and recreate
                    if len(existing_answers) > 1:
                        logger.warning(
                            f"[{provider_key}] Found {len(existing_answers)} duplicate records "
                            f"for {domain}, consolidating"
                        )
                    # Delete all existing managed entries
                    for old_answer in existing_answers:
                        if provider.delete_record(domain, old_answer):
                            self._unmark_record_managed(state, domain, old_answer, provider)
                            changes += 1
                    # Re-add the single correct record
                    if provider.add_record(domain, answer):
                        self._mark_record_managed(state, domain, answer, provider)
                        changes += 1

        # Any owned record absent from this target's desired state is stale. Source
        # failures retain their last-known state, so this remains safe during outages
        # and also works when a target key is not a DNS hostname (for example golinks).
        delete_candidates = set(domains_to_delete_from_state)
        delete_candidates.update(
            domain
            for domain, answers in records_by_key.items()
            if domain not in desired
            and domain not in protected
            and any(self._is_record_managed(state, domain, answer, provider) for answer in answers)
        )

        for domain in sorted(delete_candidates):
            # Static rewrites are intentionally not auto-removed.
            if domain in protected:
                continue

            # Delete only managed records for this domain
            for old_answer in records_by_key.get(domain, []):
                if self._is_record_managed(state, domain, old_answer, provider):
                    logger.info(
                        f"[{provider_key}] Removing {provider.record_kind.lower()} "
                        f"{domain} -> {old_answer}"
                    )
                    if provider.delete_record(domain, old_answer):
                        self._unmark_record_managed(state, domain, old_answer, provider)
                        changes += 1
                else:
                    logger.debug(
                        f"[{provider_key}] Preserving pre-existing record {domain} -> {old_answer}"
                    )

        return changes

    def render_plan_once(self) -> bool:
        """Render the desired DNS state without writing provider records or state."""
        logger.info("Rendering record plan (no target or state changes will be made)")

        state = self.state_store.load()
        state.setdefault("version", 1)
        state.setdefault("instances", {})
        state.setdefault("domains", {})
        state.setdefault("managed_records", {})
        state.setdefault("managed_records_by_provider", {})
        self._migrate_legacy_managed_records(state)

        instances = self.proxy_provider.get_instances()
        desired_state: Dict[str, Any] = {"domains": {}}

        for instance in instances:
            try:
                routes = self.proxy_provider.get_routes(instance)
            except (
                requests.exceptions.RequestException,
                json.JSONDecodeError,
                RouteSourceReadError,
            ) as e:
                self._log_source_read_failure(instance, e, preserving_state=False)
                continue

            included_count = 0
            excluded_count = 0
            external_count = 0
            public_external_count = 0
            for route in routes:
                hostname = route.hostname
                if _is_domain_excluded(hostname, self.exclude_patterns):
                    excluded_count += 1
                    continue
                if route.zone == DNSZone.EXTERNAL and not route.publish_external:
                    external_count += 1
                    continue
                if route.zone == DNSZone.EXTERNAL:
                    public_external_count += 1

                domain_state = desired_state["domains"].setdefault(hostname, {"sources": {}})
                sources = domain_state.setdefault("sources", {})
                sources[instance.name] = self._route_source_state(route, int(time.time()))
                included_count += 1

            stats_parts = []
            if excluded_count:
                stats_parts.append(f"{excluded_count} excluded")
            if external_count:
                stats_parts.append(f"{external_count} external")
            if public_external_count:
                stats_parts.append(f"{public_external_count} public external")
            stats_msg = f" ({', '.join(stats_parts)})" if stats_parts else ""
            logger.info(f"Source '{instance.name}': {included_count} routes{stats_msg}")

        success = True
        for provider in self.record_targets:
            provider_key = self._target_log_label(provider)
            desired, protected_records = self._compute_desired_records_for_provider(
                provider, desired_state, instances
            )
            logger.info(f"[{provider_key}] Desired records: {len(desired)}")
            try:
                all_records = provider.get_records()
            except Exception as e:
                self._log_target_read_failure(provider, "plan rendering", e, plan=True)
                success = False
                continue

            records_by_key: Dict[str, List[str]] = {}
            for record in all_records:
                records_by_key.setdefault(record.key, []).append(record.value)

            create_count = 0
            ok_count = 0
            update_count = 0
            conflict_count = 0
            delete_count = 0

            logger.info(f"[{provider_key}] Plan:")
            for domain, answer in sorted(desired.items()):
                existing_answers = records_by_key.get(domain, [])
                if not existing_answers:
                    create_count += 1
                    logger.info(f"[{provider_key}]   CREATE   {domain} -> {answer}")
                    continue

                if existing_answers == [answer]:
                    ok_count += 1
                    logger.info(f"[{provider_key}]   OK       {domain} -> {answer}")
                    continue

                if answer in existing_answers:
                    ok_count += 1
                    logger.info(
                        f"[{provider_key}]   OK       {domain} -> {answer} "
                        f"(existing answers: {existing_answers})"
                    )
                    continue

                managed_answers = [
                    existing
                    for existing in existing_answers
                    if self._is_record_managed(state, domain, existing, provider)
                ]
                if self.takeover_existing_records:
                    update_count += 1
                    logger.info(
                        f"[{provider_key}]   TAKEOVER {domain}: {existing_answers} -> {answer}"
                    )
                    continue
                if managed_answers and len(managed_answers) == len(existing_answers):
                    update_count += 1
                    logger.info(
                        f"[{provider_key}]   UPDATE   {domain}: {existing_answers} -> {answer}"
                    )
                else:
                    conflict_count += 1
                    logger.warning(
                        f"[{provider_key}]   CONFLICT {domain}: existing "
                        f"{existing_answers}, desired {answer}"
                    )

            for domain, existing_answers in sorted(records_by_key.items()):
                if domain in desired or domain in protected_records:
                    continue
                if provider.applies_domain_exclusions and _is_domain_excluded(
                    domain, self.exclude_patterns
                ):
                    continue
                for existing in existing_answers:
                    if self._is_record_managed(state, domain, existing, provider):
                        delete_count += 1
                        logger.info(f"[{provider_key}]   DELETE   {domain} -> {existing}")

            logger.info(
                f"[{provider_key}] Summary: {ok_count} ok, {create_count} create, "
                f"{update_count} update, {delete_count} delete, {conflict_count} conflict"
            )

        return success

    def sync_once(self) -> bool:
        """Run a single sync cycle.

        Returns:
            True if any DNS records were added, updated, or deleted; False otherwise.
        """
        changes_made = 0
        now = int(time.time())
        state = self.state_store.load()
        state.setdefault("version", 1)
        state.setdefault("instances", {})
        state.setdefault("domains", {})
        state.setdefault("managed_records", {})
        state.setdefault("managed_records_by_provider", {})
        self._migrate_legacy_managed_records(state)

        instances = self.proxy_provider.get_instances()

        # On first sync after startup, clean up records from removed proxy instances
        if not self._startup_cleanup_done:
            changes_made += self._cleanup_removed_instances(state, instances)
            self._startup_cleanup_done = True

        # Ensure static rewrites first.
        changes_made += self._sync_static_rewrites(state)

        instance_success: Dict[str, bool] = {}
        instance_seen_domains: Dict[str, Set[str]] = {}

        for instance in instances:
            try:
                routes = self.proxy_provider.get_routes(instance)

                seen: Set[str] = set()
                excluded_count = 0
                external_count = 0
                public_external_count = 0
                for route in routes:
                    hostname = route.hostname
                    # Skip domains matching exclusion patterns
                    if _is_domain_excluded(hostname, self.exclude_patterns):
                        excluded_count += 1
                        logger.debug(f"Excluding domain '{hostname}' (matches exclusion pattern)")
                        continue
                    # Skip external zone domains unless explicitly mapped to a public answer.
                    if route.zone == DNSZone.EXTERNAL and not route.publish_external:
                        external_count += 1
                        logger.debug(
                            f"Skipping external zone domain '{hostname}' "
                            f"(router: {route.router_name}, forwarded to upstream DNS)"
                        )
                        continue
                    if route.zone == DNSZone.EXTERNAL:
                        public_external_count += 1
                        logger.debug(
                            f"Publishing external zone domain '{hostname}' "
                            f"(router: {route.router_name}) to configured public DNS answer"
                        )
                    seen.add(hostname)
                    domain_state = state["domains"].setdefault(hostname, {"sources": {}})
                    sources = domain_state.setdefault("sources", {})
                    sources[instance.name] = self._route_source_state(route, now)

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
                if public_external_count:
                    stats_parts.append(f"{public_external_count} public external")
                stats_msg = f" ({', '.join(stats_parts)})" if stats_parts else ""
                logger.info(f"Source '{instance.name}': {len(seen)} hostnames{stats_msg}")

            except (
                requests.exceptions.RequestException,
                json.JSONDecodeError,
                RouteSourceReadError,
            ) as e:
                instance_success[instance.name] = False
                instance_seen_domains[instance.name] = set()
                error_detail = _concise_error(e)
                prev = state["instances"].get(instance.name, {})
                state["instances"][instance.name] = {
                    "last_success": prev.get("last_success", 0),
                    "last_error": error_detail,
                    "url": instance.url,
                }
                self._log_source_read_failure(instance, e, preserving_state=True)

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

        for provider in self.record_targets:
            desired, protected_records = self._compute_desired_records_for_provider(
                provider, state, instances
            )
            changes_made += self._reconcile_record_target(
                provider,
                state,
                desired,
                domains_to_delete_from_state,
                protected_records,
            )

        for domain in sorted(domains_to_delete_from_state):
            if domain not in self.static_rewrites:
                state["domains"].pop(domain, None)

        if self.exclude_patterns:
            for domain in list(state["domains"].keys()):
                if domain not in self.static_rewrites and _is_domain_excluded(
                    domain, self.exclude_patterns
                ):
                    state["domains"].pop(domain, None)

        self.state_store.save(state)

        if changes_made > 0:
            logger.info(f"Sync completed with {changes_made} managed record change(s)")

        return changes_made > 0


def _build_static_rewrites(
    settings: RuntimeSettings, instances: List[ProxyInstance]
) -> Dict[str, str]:
    """Build static rewrite answers, using the first source target as default."""
    default_ip_for_static = instances[0].target_ip if instances else ""
    static_rewrites: Dict[str, str] = {}
    for domain, answer in settings.static_rewrites.items():
        if answer:
            static_rewrites[domain] = answer
        elif default_ip_for_static:
            static_rewrites[domain] = default_ip_for_static
    return static_rewrites


def _apply_runtime_config_to_syncer(
    syncer: ExternalDNSSyncer,
    *,
    dns_providers: List[DNSProvider],
    proxy_provider: ReverseProxyProvider,
    settings: RuntimeSettings,
    instances: List[ProxyInstance],
) -> None:
    """Apply reloaded runtime inputs and force removed-source cleanup next sync."""
    if not dns_providers:
        raise ValueError("At least one provider is required")

    syncer.record_targets = list(dns_providers)
    syncer.record_target = dns_providers[0]
    syncer.dns_providers = list(dns_providers)
    syncer.dns_provider = dns_providers[0]
    syncer.proxy_provider = proxy_provider
    syncer.static_rewrites = _build_static_rewrites(settings, instances)
    syncer.exclude_patterns = _parse_exclude_patterns(settings.exclude_domains)
    syncer.takeover_existing_records = settings.takeover_existing_records
    syncer._startup_cleanup_done = False


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

    # Validate provider config
    provider_configs = yaml_providers if using_yaml_config else [_dns_provider_config_from_env()]
    for provider_config in provider_configs:
        provider_type = provider_config.provider.lower().strip()
        provider_label = provider_config.name or provider_type or "default"

        if provider_type not in ["adguard", "technitium", "goku"]:
            errors.append(
                f"Provider '{provider_label}' has unsupported provider type: "
                f"{provider_type}. Supported: adguard, technitium, goku"
            )
            continue

        if not provider_config.url:
            if using_yaml_config:
                errors.append(
                    f"Provider '{provider_label}' URL is required. "
                    f"Set providers[].url in {CONFIG_PATH}."
                )
            elif provider_type == "technitium":
                errors.append(
                    "Technitium URL is required. "
                    "Set via YAML config (providers[].url) or TECHNITIUM_URL env var."
                )
            elif provider_type == "goku":
                errors.append(
                    "Goku URL is required. "
                    "Set via YAML config (providers[].url) or GOKU_URL env var."
                )
            else:
                errors.append(
                    "Provider URL is required. "
                    "Set via YAML config (providers[].url) or ADGUARD_URL env var."
                )
            continue

        if provider_type == "technitium":
            if not provider_config.api_token:
                errors.append(
                    f"Provider '{provider_label}' Technitium api_token is required. "
                    "Set via YAML config (providers[].api_token) or TECHNITIUM_API_TOKEN env var."
                )
            if not provider_config.zones:
                errors.append(
                    f"Provider '{provider_label}' Technitium zones are required. "
                    "Set via YAML config (providers[].zones) or TECHNITIUM_ZONES env var."
                )
        elif provider_type == "goku":
            if not provider_config.api_token and not (
                provider_config.username and provider_config.password
            ):
                logger.warning(
                    f"Provider '{provider_label}' Goku authentication not set. "
                    "Using unauthenticated access."
                )
        elif not provider_config.username or not provider_config.password:
            if using_yaml_config:
                logger.warning(
                    f"Provider '{provider_label}' username/password not set in config. "
                    "Using unauthenticated access."
                )
            else:
                logger.warning("ADGUARD_USERNAME/PASSWORD not set. Using unauthenticated access.")

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

    logger.info(f"external-dns starting with route source type: {PROXY_PROVIDER}")

    if "--validate-config" in sys.argv:
        if not validate_config():
            logger.error("Configuration validation failed")
            sys.exit(1)

        dns_provider_configs = load_dns_providers_from_yaml(CONFIG_PATH)
        if not dns_provider_configs:
            dns_provider_configs = [_dns_provider_config_from_env()]
        dns_providers = create_record_targets()
        proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
        instances = proxy_provider.get_instances()

        logger.info(f"Configured {len(dns_providers)} provider(s):")
        for config, provider in zip(dns_provider_configs, dns_providers, strict=True):
            logger.info(f"  - {config.name}: {provider.name} ({config.url})")
        logger.info(f"Configured {len(instances)} route source(s):")
        for inst in instances:
            logger.info(f"  - {inst.name}: {inst.url} -> {inst.target_ip}")
        logger.info(
            "Takeover existing records: "
            f"{'enabled' if settings.takeover_existing_records else 'disabled'}"
        )

        failed_providers = [
            provider.name for provider in dns_providers if not provider.test_connection()
        ]
        if failed_providers:
            logger.error(f"Provider connection failed: {', '.join(failed_providers)}")
            sys.exit(1)

        logger.info("Configuration validation successful")
        return

    # Validate configuration
    if not validate_config():
        logger.error("Configuration validation failed")
        sys.exit(1)

    # Create providers
    dns_provider_configs = load_dns_providers_from_yaml(CONFIG_PATH)
    if not dns_provider_configs:
        dns_provider_configs = [_dns_provider_config_from_env()]
    dns_providers = create_record_targets()
    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
    instances = proxy_provider.get_instances()

    logger.info(f"Configured {len(dns_providers)} provider(s):")
    for config, provider in zip(dns_provider_configs, dns_providers, strict=True):
        logger.info(f"  - {config.name}: {provider.name} ({config.url})")
    logger.info(f"Route source provider: {proxy_provider.name}")
    logger.info(f"Configured {len(instances)} route source(s):")
    for inst in instances:
        logger.info(f"  - {inst.name}: {inst.url} -> {inst.target_ip}")
    logger.info(f"Default zone: {settings.default_zone} (only 'internal' zones sync to local DNS)")
    logger.info(
        "Takeover existing records: "
        f"{'enabled' if settings.takeover_existing_records else 'disabled'}"
    )
    logger.info(f"Sync mode: {settings.sync_mode}")
    if settings.sync_mode == "watch":
        logger.info(f"Poll interval: {settings.poll_interval}s")
        config_files = find_config_files(CONFIG_PATH)
        if len(config_files) > 1:
            logger.info(f"Config watch: scanning {len(config_files)} files in {CONFIG_PATH}")
        else:
            logger.info(f"Config watch: enabled for {CONFIG_PATH}")

    # Process static rewrites from settings (fill in default IP for entries without one)
    static_rewrites = _build_static_rewrites(settings, instances)
    if static_rewrites:
        logger.info(f"Static rewrites: {', '.join(sorted(static_rewrites.keys()))}")

    # Parse domain exclusion patterns
    exclude_patterns = _parse_exclude_patterns(settings.exclude_domains)
    if exclude_patterns:
        logger.info(f"Domain exclusions: {len(exclude_patterns)} pattern(s) configured")

    # Log webhook configuration
    if settings.webhook.enabled:
        logger.info(f"Webhook: {settings.webhook.method} {settings.webhook.url}")
        if settings.webhook.only_on_changes:
            logger.info("  - Triggered only when managed records change")
        else:
            logger.info("  - Triggered on every sync cycle")

    # Test connections. Continue when at least one provider is reachable so
    # temporarily failed targets can recover in watch mode.
    successful_dns_connections = 0
    for provider in dns_providers:
        if provider.test_connection():
            successful_dns_connections += 1
    if successful_dns_connections == 0:
        logger.error("Cannot connect to any configured provider. Exiting.")
        sys.exit(1)

    syncer = ExternalDNSSyncer(
        record_targets=dns_providers,
        proxy_provider=proxy_provider,
        state_store=StateStore(STATE_PATH),
        static_rewrites=static_rewrites,
        exclude_patterns=exclude_patterns,
        takeover_existing_records=settings.takeover_existing_records,
    )

    if "--render-plan" in sys.argv:
        if not syncer.render_plan_once():
            sys.exit(1)
        return

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Helper function to run sync and trigger webhook if appropriate
    def run_sync_with_webhook() -> bool:
        """Run sync and call webhook if configured.

        Returns:
            True if sync completed successfully with changes, False otherwise.
        """
        try:
            changes_made = syncer.sync_once()

            # Call webhook if configured
            if settings.webhook.enabled:
                should_call = not settings.webhook.only_on_changes or changes_made
                if should_call:
                    call_webhook(
                        url=settings.webhook.url,
                        method=settings.webhook.method,
                        username=settings.webhook.username,
                        password=settings.webhook.password,
                        timeout=settings.webhook.timeout,
                    )

            return changes_made
        except Exception as e:
            raise e

    # Run sync
    try:
        if settings.sync_mode == "once":
            run_sync_with_webhook()
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
                run_sync_with_webhook()
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

                # Recreate providers with new config
                try:
                    # Reload settings for any changes
                    settings = load_settings_from_yaml(CONFIG_PATH)
                    dns_provider_configs = load_dns_providers_from_yaml(CONFIG_PATH)
                    if not dns_provider_configs:
                        dns_provider_configs = [_dns_provider_config_from_env()]
                    dns_providers = create_record_targets()
                    successful_dns_connections = 0
                    for provider in dns_providers:
                        if provider.test_connection():
                            successful_dns_connections += 1
                    if successful_dns_connections == 0:
                        raise RuntimeError("Cannot connect to any configured provider")

                    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
                    instances = proxy_provider.get_instances()
                    logger.info(
                        f"Reloaded {len(instances)} route source(s): "
                        f"{', '.join([i.name for i in instances])}"
                    )

                    # Update syncer with all reloaded runtime inputs.
                    _apply_runtime_config_to_syncer(
                        syncer,
                        dns_providers=dns_providers,
                        proxy_provider=proxy_provider,
                        settings=settings,
                        instances=instances,
                    )

                    # Trigger immediate sync after config reload
                    logger.info("Triggering immediate sync after config reload")
                    run_sync_with_webhook()
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
