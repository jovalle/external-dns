#!/usr/bin/env python3
"""external-dns - Universal DNS Synchronization

Syncs reverse proxy routes into DNS providers, similar in spirit to Kubernetes
external-dns. Supports multiple DNS providers and reverse proxy implementations.

Supported DNS Providers:
    - adguard: AdGuard Home DNS rewrites
    - technitium: Technitium DNS Server A records
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

    DNS Provider Configuration (providers section):
        name            Friendly name for this provider instance
        provider        Provider type: "adguard" (default) or "technitium"
        url             Provider API URL (required)
        username        API username (optional, adguard basic auth)
        password        API password (optional, adguard basic auth)
        api_token       API token (technitium; sent as Authorization: Bearer)
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
from dataclasses import dataclass, field
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
class DNSProviderConfig:
    """Configuration for a DNS provider."""

    name: str
    provider: str  # adguard, cloudflare, etc.
    url: str
    username: str = ""
    password: str = ""
    api_token: str = ""  # For providers that use API tokens
    zones: List[str] = field(default_factory=list)


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
                        providers.append(
                            DNSProviderConfig(
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
    publish_external: bool = False


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


class DNSProviderReadError(Exception):
    """Raised when provider records cannot be read safely."""


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
            raise DNSProviderReadError(str(e)) from e

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


class TechnitiumAPIError(Exception):
    """Recoverable Technitium API status error."""


class TechnitiumDNSProvider(DNSProvider):
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
            logger.error(f"Technitium A-record write failed for {domain_l} at {self._url}: {e}")
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
            logger.error(f"Failed to connect to {self.name} at {self._url}: {e}")
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
            logger.error(f"Failed to get records from {self.name} at {self._url}: {e}")
            raise DNSProviderReadError(str(e)) from e

        return records

    def add_record(self, domain: str, answer: str) -> bool:
        if not domain or not answer:
            return False
        result = self._write_a_record(
            "/api/zones/records/add",
            domain,
            {"ipAddress": answer.strip()},
        )
        if result:
            logger.info(f"Added Technitium A record: {domain} -> {answer}")
        return result

    def delete_record(self, domain: str, answer: str) -> bool:
        if not domain or not answer:
            return False
        result = self._write_a_record(
            "/api/zones/records/delete",
            domain,
            {"ipAddress": answer.strip()},
        )
        if result:
            logger.info(f"Deleted Technitium A record: {domain} -> {answer}")
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
        if result:
            logger.info(f"Updated Technitium A record: {domain} -> {new_answer}")
        return result


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

    HOST_CALL_RE = re.compile(r"Host\(([^)]*)\)")
    HOST_ARG_RE = re.compile(r"[`\"\']([^`\"\']+)[`\"\']")
    ZONE_SUFFIX_RE = re.compile(r"-(internal|external)(?:@|$)", re.IGNORECASE)

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
                                )
                            )
                    except Exception as e:
                        logger.error(f"Failed to load config from {config_file}: {e}")

                if all_instances:
                    logger.info(
                        f"Loaded {len(all_instances)} Traefik instance(s) from {len(config_files)} config file(s)"
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
            publish_external = zone == DNSZone.EXTERNAL and bool(instance.public_target_ip)
            route_target_ip = instance.public_target_ip if publish_external else instance.target_ip

            for hostname in self._extract_hostnames(rule):
                routes.append(
                    ProxyRoute(
                        hostname=hostname,
                        source_name=instance.name,
                        target_ip=route_target_ip,
                        zone=zone,
                        router_name=router_name,
                        publish_external=publish_external,
                    )
                )
        return routes

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
                hostname = match.group(1).strip()
                if hostname:
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

    return {
        "provider": DNS_PROVIDER,
        "url": ADGUARD_URL,
        "username": ADGUARD_USERNAME,
        "password": ADGUARD_PASSWORD,
        "api_token": "",
        "zones": [],
    }


def _dns_provider_config_from_env() -> DNSProviderConfig:
    """Build a single DNS provider config from environment fallback variables."""
    config = get_dns_config()
    provider_type = str(config.get("provider") or DNS_PROVIDER).lower().strip()
    return DNSProviderConfig(
        name=provider_type or "default",
        provider=provider_type,
        url=str(config.get("url") or "").strip(),
        username=str(config.get("username") or "").strip(),
        password=str(config.get("password") or "").strip(),
        api_token=str(config.get("api_token") or "").strip(),
        zones=_parse_dns_zones(config.get("zones")),
    )


def _create_dns_provider_from_config(config: DNSProviderConfig) -> DNSProvider:
    """Create a concrete DNS provider from normalized config."""
    provider_type = config.provider.lower().strip()
    supported = ["adguard", "technitium"]

    if provider_type == "adguard":
        return AdGuardDNSProvider(config.url, config.username, config.password)
    if provider_type == "technitium":
        return TechnitiumDNSProvider(config.url, config.api_token, config.zones)
    raise ValueError(
        f"Unsupported DNS provider type '{provider_type}' for provider '{config.name}'. "
        f"Supported providers: {', '.join(supported)}."
    )


def create_dns_providers() -> List[DNSProvider]:
    """Factory function to create all configured DNS providers."""
    yaml_providers = load_dns_providers_from_yaml(CONFIG_PATH)
    provider_configs = yaml_providers if yaml_providers else [_dns_provider_config_from_env()]
    return [_create_dns_provider_from_config(config) for config in provider_configs]


def create_dns_provider() -> DNSProvider:
    """Factory function to create the first configured DNS provider."""
    try:
        return create_dns_providers()[0]
    except ValueError as e:
        supported = ["adguard", "technitium"]
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
        dns_provider: Optional[DNSProvider] = None,
        dns_providers: Optional[List[DNSProvider]] = None,
        proxy_provider: ReverseProxyProvider,
        state_store: StateStore,
        static_rewrites: Dict[str, str],
        exclude_patterns: List[re.Pattern],
        takeover_existing_records: bool = False,
    ):
        if dns_providers is not None:
            providers = list(dns_providers)
        elif dns_provider is not None:
            providers = [dns_provider]
        else:
            raise ValueError("At least one DNS provider is required")
        if not providers:
            raise ValueError("At least one DNS provider is required")

        self.dns_providers = providers
        self.dns_provider = providers[0]
        self.proxy_provider = proxy_provider
        self.state_store = state_store
        self.static_rewrites = static_rewrites
        self.exclude_patterns = exclude_patterns
        self.takeover_existing_records = takeover_existing_records
        self._startup_cleanup_done = False

    def _provider_state_key(self, provider: DNSProvider) -> str:
        """Return a stable, non-secret provider identity for managed state."""
        identity = getattr(provider, "_url", "") or getattr(provider, "url", "")
        if identity:
            return f"{provider.name}:{identity}"
        return provider.name

    def _is_record_managed(
        self,
        state: Dict[str, Any],
        domain: str,
        answer: str,
        provider: Optional[DNSProvider] = None,
    ) -> bool:
        """Check if a DNS record was created by external-dns."""
        dns_provider = provider or self.dns_provider
        managed_by_provider = state.get("managed_records_by_provider", {})
        if isinstance(managed_by_provider, dict) and managed_by_provider:
            provider_key = self._provider_state_key(dns_provider)
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
            or not self.dns_providers
        ):
            return

        provider_key = self._provider_state_key(self.dns_providers[0])
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
        provider: Optional[DNSProvider] = None,
    ) -> None:
        """Track a DNS record as managed by external-dns."""
        dns_provider = provider or self.dns_provider
        managed_by_provider = state.setdefault("managed_records_by_provider", {})
        provider_key = self._provider_state_key(dns_provider)
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
        provider: Optional[DNSProvider] = None,
    ) -> None:
        """Remove a DNS record from managed tracking."""
        dns_provider = provider or self.dns_provider
        managed_by_provider = state.get("managed_records_by_provider", {})
        if isinstance(managed_by_provider, dict):
            provider_key = self._provider_state_key(dns_provider)
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
        self, state: Dict[str, Any], provider: DNSProvider
    ) -> int:
        """Sync static rewrites and return the number of changes made."""
        if not self.static_rewrites:
            return 0

        changes = 0
        try:
            current_records = {r.domain: r.answer for r in provider.get_records()}
        except Exception as e:
            logger.error(
                f"[{self._provider_state_key(provider)}] Failed to get DNS records "
                f"for static rewrites: {e}"
            )
            return 0

        for domain, answer in self.static_rewrites.items():
            if domain in current_records:
                current_answer = current_records[domain]
                if current_answer == answer:
                    # Record already exists with correct answer - mark as managed
                    self._mark_record_managed(state, domain, answer, provider)
                elif self._is_record_managed(state, domain, current_answer, provider):
                    # Record is managed by us with wrong answer - update it
                    logger.info(
                        f"[{self._provider_state_key(provider)}] Updating static rewrite "
                        f"{domain}: {current_answer} -> {answer}"
                    )
                    if provider.update_record(domain, current_answer, answer):
                        self._unmark_record_managed(state, domain, current_answer, provider)
                        self._mark_record_managed(state, domain, answer, provider)
                        changes += 1
                elif self.takeover_existing_records:
                    logger.info(
                        f"[{self._provider_state_key(provider)}] Taking ownership of static "
                        f"rewrite {domain}: {current_answer} -> {answer}"
                    )
                    if provider.update_record(domain, current_answer, answer):
                        self._mark_record_managed(state, domain, answer, provider)
                        changes += 1
                else:
                    # Pre-existing record not managed by us - warn and skip
                    logger.warning(
                        f"[{self._provider_state_key(provider)}] Static rewrite "
                        f"{domain} -> {answer} conflicts with pre-existing "
                        f"record {domain} -> {current_answer} (not managed by external-dns, skipping)"
                    )
            else:
                logger.info(
                    f"[{self._provider_state_key(provider)}] Adding static rewrite {domain} -> {answer}"
                )
                if provider.add_record(domain, answer):
                    self._mark_record_managed(state, domain, answer, provider)
                    changes += 1

        return changes

    def _sync_static_rewrites(self, state: Dict[str, Any]) -> int:
        """Sync static rewrites across all configured DNS providers."""
        changes = 0
        for provider in self.dns_providers:
            changes += self._sync_static_rewrites_for_provider(state, provider)
        return changes

    def _cleanup_removed_instances(
        self, state: Dict[str, Any], instances: List[ProxyInstance]
    ) -> int:
        """Remove all DNS records from proxy instances that are no longer configured.

        Returns:
            Number of DNS record changes made.
        """
        configured_names = {i.name for i in instances}
        state_instances = state.get("instances", {})
        removed_instances = set(state_instances.keys()) - configured_names

        if not removed_instances:
            return 0

        changes = 0
        logger.info(f"Detected removed proxy instances: {', '.join(sorted(removed_instances))}")

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
        for provider in self.dns_providers:
            records_by_domain: Dict[str, List[str]] = {}
            try:
                for r in provider.get_records():
                    records_by_domain.setdefault(r.domain, []).append(r.answer)
            except Exception as e:
                logger.error(
                    f"[{self._provider_state_key(provider)}] Failed to get DNS records "
                    f"for removed-instance cleanup: {e}"
                )
                continue

            for domain in sorted(domains_to_cleanup):
                # Don't remove static rewrites
                if domain in self.static_rewrites:
                    logger.debug(f"Skipping static rewrite '{domain}' during instance cleanup")
                    continue

                for answer in records_by_domain.get(domain, []):
                    if self._is_record_managed(state, domain, answer, provider):
                        logger.info(
                            f"[{self._provider_state_key(provider)}] Removing orphaned record "
                            f"from removed instance: {domain} -> {answer}"
                        )
                        if provider.delete_record(domain, answer):
                            self._unmark_record_managed(state, domain, answer, provider)
                            changes += 1
                    else:
                        logger.debug(
                            f"[{self._provider_state_key(provider)}] Skipping pre-existing "
                            f"record during instance cleanup: {domain} -> {answer}"
                        )

        for domain in sorted(domains_to_cleanup):
            state["domains"].pop(domain, None)

        # Remove the instance entries from state
        for removed_name in removed_instances:
            state["instances"].pop(removed_name, None)
            logger.info(f"Cleaned up state for removed instance: {removed_name}")

        return changes

    def _reconcile_dns_provider(
        self,
        provider: DNSProvider,
        state: Dict[str, Any],
        desired: Dict[str, str],
        domains_to_delete_from_state: List[str],
    ) -> int:
        """Reconcile desired DNS records against one provider's current records."""
        provider_key = self._provider_state_key(provider)
        changes = 0
        try:
            all_records = provider.get_records()
        except Exception as e:
            logger.error(f"[{provider_key}] Failed to get DNS records for reconciliation: {e}")
            return 0

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
                    # Remove from records_by_domain so we don't process it later
                    if deleted_any:
                        del records_by_domain[domain]

        # Apply creates/updates, handling duplicates (respecting managed records).
        for domain, answer in sorted(desired.items()):
            existing_answers = records_by_domain.get(domain, [])

            if not existing_answers:
                # No existing record - add it and mark as managed
                logger.info(f"[{provider_key}] Adding record {domain} -> {answer}")
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
                            f"[{provider_key}] Domain {domain} has pre-existing record(s) "
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

        # Apply deletions for domains that now have no sources AND were confirmed absent.
        for domain in sorted(domains_to_delete_from_state):
            # Static rewrites are intentionally not auto-removed.
            if domain in self.static_rewrites:
                continue

            # Delete only managed records for this domain
            for old_answer in records_by_domain.get(domain, []):
                if self._is_record_managed(state, domain, old_answer, provider):
                    logger.info(f"[{provider_key}] Removing record {domain} -> {old_answer}")
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
        logger.info("Rendering DNS plan (no DNS or state changes will be made)")

        state = self.state_store.load()
        state.setdefault("version", 1)
        state.setdefault("instances", {})
        state.setdefault("domains", {})
        state.setdefault("managed_records", {})
        state.setdefault("managed_records_by_provider", {})
        self._migrate_legacy_managed_records(state)

        instances = self.proxy_provider.get_instances()
        desired_sources: Dict[str, Dict[str, str]] = {}

        for domain, answer in self.static_rewrites.items():
            desired_sources.setdefault(domain, {})["static"] = answer

        for instance in instances:
            try:
                routes = self.proxy_provider.get_routes(instance)
            except requests.exceptions.RequestException as e:
                logger.warning(
                    f"Proxy instance '{instance.name}' ({instance.url}) unreachable: {e}"
                )
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

                desired_sources.setdefault(hostname, {})[instance.name] = route.target_ip
                included_count += 1

            stats_parts = []
            if excluded_count:
                stats_parts.append(f"{excluded_count} excluded")
            if external_count:
                stats_parts.append(f"{external_count} external")
            if public_external_count:
                stats_parts.append(f"{public_external_count} public external")
            stats_msg = f" ({', '.join(stats_parts)})" if stats_parts else ""
            logger.info(f"Proxy instance '{instance.name}': {included_count} DNS routes{stats_msg}")

        desired: Dict[str, str] = {}
        configured_order = [i.name for i in instances]
        for domain, sources in desired_sources.items():
            if "static" in sources:
                desired[domain] = sources["static"]
                continue

            chosen_source = ""
            for source_name in configured_order:
                if source_name in sources:
                    chosen_source = source_name
                    desired[domain] = sources[source_name]
                    break

            distinct_answers = sorted({answer for answer in sources.values() if answer})
            if len(distinct_answers) > 1:
                logger.warning(
                    f"Domain '{domain}' present on multiple proxy instances with different "
                    f"target IPs {distinct_answers}; using '{desired[domain]}' "
                    f"from '{chosen_source}'"
                )

        logger.info(f"Desired DNS records: {len(desired)}")

        success = True
        for provider in self.dns_providers:
            provider_key = self._provider_state_key(provider)
            try:
                all_records = provider.get_records()
            except Exception as e:
                logger.error(f"[{provider_key}] Failed to get DNS records for plan: {e}")
                success = False
                continue

            records_by_domain: Dict[str, List[str]] = {}
            for record in all_records:
                records_by_domain.setdefault(record.domain, []).append(record.answer)

            create_count = 0
            ok_count = 0
            update_count = 0
            conflict_count = 0
            delete_count = 0

            logger.info(f"[{provider_key}] Plan:")
            for domain, answer in sorted(desired.items()):
                existing_answers = records_by_domain.get(domain, [])
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

            for domain, existing_answers in sorted(records_by_domain.items()):
                if domain in desired or domain in self.static_rewrites:
                    continue
                if _is_domain_excluded(domain, self.exclude_patterns):
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
                if public_external_count:
                    stats_parts.append(f"{public_external_count} public external")
                stats_msg = f" ({', '.join(stats_parts)})" if stats_parts else ""
                logger.info(f"Proxy instance '{instance.name}': {len(seen)} DNS domains{stats_msg}")

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

        for provider in self.dns_providers:
            changes_made += self._reconcile_dns_provider(
                provider,
                state,
                desired,
                domains_to_delete_from_state,
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
            logger.info(f"Sync completed with {changes_made} DNS record change(s)")

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
        raise ValueError("At least one DNS provider is required")

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

    # Validate DNS provider config
    provider_configs = yaml_providers if using_yaml_config else [_dns_provider_config_from_env()]
    for provider_config in provider_configs:
        provider_type = provider_config.provider.lower().strip()
        provider_label = provider_config.name or provider_type or "default"

        if provider_type not in ["adguard", "technitium"]:
            errors.append(
                f"DNS provider '{provider_label}' has unsupported provider type: "
                f"{provider_type}. Supported: adguard, technitium"
            )
            continue

        if not provider_config.url:
            if using_yaml_config:
                errors.append(
                    f"DNS provider '{provider_label}' URL is required. "
                    f"Set providers[].url in {CONFIG_PATH}."
                )
            elif provider_type == "technitium":
                errors.append(
                    "Technitium URL is required. "
                    "Set via YAML config (providers[].url) or TECHNITIUM_URL env var."
                )
            else:
                errors.append(
                    "DNS provider URL is required. "
                    "Set via YAML config (providers[].url) or ADGUARD_URL env var."
                )
            continue

        if provider_type == "technitium":
            if not provider_config.api_token:
                errors.append(
                    f"DNS provider '{provider_label}' Technitium api_token is required. "
                    "Set via YAML config (providers[].api_token) or TECHNITIUM_API_TOKEN env var."
                )
            if not provider_config.zones:
                errors.append(
                    f"DNS provider '{provider_label}' Technitium zones are required. "
                    "Set via YAML config (providers[].zones) or TECHNITIUM_ZONES env var."
                )
        elif not provider_config.username or not provider_config.password:
            if using_yaml_config:
                logger.warning(
                    f"⚠️  DNS provider '{provider_label}' username/password not set in config. "
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

    if "--validate-config" in sys.argv:
        if not validate_config():
            logger.error("Configuration validation failed")
            sys.exit(1)

        dns_provider_configs = load_dns_providers_from_yaml(CONFIG_PATH)
        if not dns_provider_configs:
            dns_provider_configs = [_dns_provider_config_from_env()]
        dns_providers = create_dns_providers()
        proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
        instances = proxy_provider.get_instances()

        logger.info(f"Configured {len(dns_providers)} DNS provider(s):")
        for config, provider in zip(dns_provider_configs, dns_providers, strict=True):
            logger.info(f"  - {config.name}: {provider.name} ({config.url})")
        logger.info(f"Configured {len(instances)} proxy instance(s):")
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
            logger.error(f"DNS provider connection failed: {', '.join(failed_providers)}")
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
    dns_providers = create_dns_providers()
    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
    instances = proxy_provider.get_instances()

    logger.info(f"Configured {len(dns_providers)} DNS provider(s):")
    for config, provider in zip(dns_provider_configs, dns_providers, strict=True):
        logger.info(f"  - {config.name}: {provider.name} ({config.url})")
    logger.info(f"Proxy Provider: {proxy_provider.name}")
    logger.info(f"Configured {len(instances)} proxy instance(s):")
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
            logger.info("  - Triggered only when DNS records change")
        else:
            logger.info("  - Triggered on every sync cycle")

    # Test connections. Continue when at least one DNS provider is reachable so
    # temporarily failed targets can recover in watch mode.
    successful_dns_connections = 0
    for provider in dns_providers:
        if provider.test_connection():
            successful_dns_connections += 1
        else:
            logger.warning(
                f"Cannot connect to {provider.name}; sync will continue for other providers."
            )
    if successful_dns_connections == 0:
        logger.error("Cannot connect to any configured DNS provider. Exiting.")
        sys.exit(1)

    syncer = ExternalDNSSyncer(
        dns_providers=dns_providers,
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
                    dns_providers = create_dns_providers()
                    successful_dns_connections = 0
                    for provider in dns_providers:
                        if provider.test_connection():
                            successful_dns_connections += 1
                        else:
                            logger.warning(
                                f"Cannot connect to {provider.name}; "
                                "sync will continue for other providers."
                            )
                    if successful_dns_connections == 0:
                        raise RuntimeError("Cannot connect to any configured DNS provider")

                    proxy_provider = create_proxy_provider(default_zone=settings.default_zone)
                    instances = proxy_provider.get_instances()
                    logger.info(
                        f"Reloaded {len(instances)} instance(s): "
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
