# external-dns

Route synchronization service that publishes reverse proxy routes to DNS and other targets, inspired by [Kubernetes external-dns](https://github.com/kubernetes-sigs/external-dns).

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f4a1/512.gif" width="32" height="32" alt="light bulb"> Overview

external-dns automatically discovers hostnames from reverse proxy configuration and reconciles corresponding records in each configured provider. DNS providers receive address records; Goku receives golink aliases.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f680/512.gif" width="32" height="32" alt="rocket"> Supported Providers

### Providers (DNS and other)

| Provider     | Status    | Environment Prefix |
| ------------ | --------- | ------------------ |
| AdGuard Home | Supported | `ADGUARD_`         |
| CoreDNS      | Candidate | -                  |
| Goku         | Supported | `GOKU_`            |
| Pi-hole      | Candidate | -                  |
| Technitium   | Supported | `TECHNITIUM_`      |

### Reverse Proxy Providers

| Provider            | Status    | Environment Prefix |
| ------------------- | --------- | ------------------ |
| Caddy               | Candidate | -                  |
| Nginx Proxy Manager | Candidate | -                  |
| Traefik             | Supported | `TRAEFIK_`         |

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/2699_fe0f/512.gif" width="32" height="32" alt="gear"> Configuration

### Provider Selection

```yaml
environment:
  DNS_PROVIDER: adguard # DNS provider type (default: adguard)
  PROXY_PROVIDER: traefik # Reverse proxy type (default: traefik)
```

### AdGuard Home DNS Provider

```yaml
environment:
  DNS_PROVIDER: adguard
  ADGUARD_URL: 'http://adguard'
  ADGUARD_USERNAME: 'admin'
  ADGUARD_PASSWORD: '${ADGUARD_ADMIN_PASSWORD}'
```

### Technitium DNS Provider

Technitium is a DNS provider only. Traefik remains the reverse proxy source that external-dns reads for route discovery.

Technitium can be selected through environment fallback:

```yaml
environment:
  DNS_PROVIDER: technitium
  TECHNITIUM_URL: 'https://dns.example.com'
  TECHNITIUM_API_TOKEN: '${TECHNITIUM_API_TOKEN}'
  TECHNITIUM_ZONES: 'example.com,internal.example.com'
```

Or through YAML provider entries:

```yaml
providers:
  - name: technitium-primary
    provider: technitium
    url: 'https://dns-a.example.com'
    api_token: '${TECHNITIUM_API_TOKEN_PRIMARY}'
    zones:
      - example.com
      - internal.example.com
```

Technitium configuration fields:

- `provider`: Must be `technitium`
- `url`: Technitium DNS Server API base URL
- `api_token`: Technitium API token; external-dns sends it as an `Authorization: Bearer` token
- `zones`: Explicit authoritative zones to manage; external-dns lists and writes A records only within these configured zones

Treat API tokens as deployment secrets. Do not commit real token values into config files.

Technitium API failures, invalid tokens, malformed JSON, HTTP errors, and missing records are treated as recoverable provider errors. In watch mode, external-dns logs the provider error and continues rather than crashing the process.

### Goku Provider

[Goku](https://github.com/jovalle/goku) is a self-hosted golinks service. It is configured just like any other provider, but receives alias records instead of DNS A records. See the [jovalle/goku](https://github.com/jovalle/goku) project for details on running Goku itself.

```yaml
providers:
  - name: golinks
    provider: goku
    url: 'https://goku.example.com'
    api_token: '${GOKU_API_TOKEN}'
```

Gotchas to make external-dns work with Goku:

- `provider` must be `goku`. `api_token` is required.
- Aliases come from Traefik service/router identity, not from Goku. See [golink aliases](#golink-aliases) for how names are generated and how conflicts are resolved.
- Static DNS rewrites are not written to Goku.

### Multiple Providers

YAML `providers:` entries are independent providers. external-dns reconciles each provider from that provider's current records, so DNS and golink providers can be kept in sync without relying on the first provider's state.

```yaml
providers:
  - name: technitium-primary
    provider: technitium
    url: 'https://dns-a.example.com'
    api_token: '${TECHNITIUM_API_TOKEN_PRIMARY}'
    zones:
      - example.com
      - internal.example.com
  - name: technitium-secondary
    provider: technitium
    url: 'https://dns-b.example.com'
    api_token: '${TECHNITIUM_API_TOKEN_SECONDARY}'
    zones:
      - example.com
      - internal.example.com
  - name: golinks
    provider: goku
    url: 'https://goku.example.com'
    api_token: '${GOKU_API_TOKEN}'
```

### Traefik Reverse Proxy Provider

**Multi-instance configuration (recommended):**

```yaml
environment:
  PROXY_PROVIDER: traefik
  TRAEFIK_INSTANCES: |
    [
      {"name": "primary", "url": "http://traefik:8080", "target_ip": "192.168.1.10"},
      {"name": "secondary", "url": "https://traefik2.example.com", "target_ip": "192.168.1.11", "verify_tls": true}
    ]
```

Each instance object supports:

- `name` (required): Unique identifier for the instance
- `url` (required): Traefik API URL
- `target_ip` (required): Internal IP address to use for internal DNS records
- `public_target_ip` (optional): Public IP address to use only for external/public routers from this source
- `verify_tls` (optional): Verify TLS certificates (default: true)
- `username` (optional): Basic auth username
- `password` (optional): Basic auth password
- `golink_alias_template` (optional): Template for Goku aliases. Supports `{app}`, `{source}`, `{hostname}`, and `{router}`. Default: `{app}`
- `golink_exclude_middlewares` (optional): Middleware names that opt a router out of Goku alias creation. Default: `no-golink`

**Single-instance configuration (legacy):**

```yaml
environment:
  TRAEFIK_URL: 'http://traefik:8080'
  TRAEFIK_TARGET_IP: '192.168.1.10'
```

### Golink Aliases

When a Goku provider is configured, all discovered routes are included in golinks by default. Aliases are derived from Traefik service identity first, then router identity if no service metadata is exposed. Generated service names such as `10-kromgo-service` are normalized to `kromgo`, and source/stack suffixes such as `dozzle-nexus` are normalized to `dozzle` when the source or Docker stack is named `nexus`. Common router entrypoint prefixes such as `websecure-` are stripped only for the router fallback path:

```text
websecure-garage-s3@docker + service garage-s3@docker + Host(`s3.example.com`) -> garage-s3, s3 -> https://s3.example.com
```

If one service exposes multiple hostnames through one or more routers on the same Traefik source, all hostnames remain eligible for DNS sync. The shared service alias is published once to the canonical hostname, while distinct hostname aliases are still published. Valid `Host()` values must be DNS hostnames; path-like values such as `nexus/ns1.example.com` are ignored.

For overlapping sources such as `mothership` and `nexus`, use source-level templates:

```yaml
sources:
  - name: mothership
    type: traefik
    url: 'https://traefik-mothership.example.com'
    target_ip: '192.168.1.10'
    golink_alias_template: '{app}-mothership'

  - name: nexus
    type: traefik
    url: 'https://traefik-nexus.example.com'
    target_ip: '192.168.1.20'
    golink_alias_template: '{app}-nexus'
```

This maps overlapping routers as `traefik-mothership` and `traefik-nexus`. When one candidate hostname basename exactly matches the alias, that canonical destination wins (for example, `kromgo` selects `kromgo.example.com` over `stat.example.com`). Otherwise, if two routes produce the same alias with different destinations, external-dns fails closed: it skips the alias, removes any previously managed conflicting alias, and logs the exact configuration options that resolve the ambiguity.

To opt a router out of golinks while keeping normal DNS sync, attach the default opt-out middleware:

```yaml
labels:
  - 'traefik.http.routers.admin.middlewares=no-golink@docker'
```

If your Traefik API exposes labels on routers, external-dns also understands:

```yaml
labels:
  - 'external-dns.golink.enabled=false'
  - 'external-dns.golink.alias=immich'
```

### Runtime Options

```yaml
environment:
  SYNC_MODE: watch # "once" or "watch" (default: watch)
  POLL_INTERVAL_SECONDS: 60 # Poll interval in watch mode (default: 60)
  LOG_LEVEL: INFO # DEBUG, INFO, WARNING, ERROR (default: INFO)
  STATE_PATH: /data/state.json # State file path (default: /data/state.json)
```

### Static Rewrites

Add DNS records that are always present, regardless of reverse proxy configuration:

```yaml
environment:
  EXTERNAL_DNS_STATIC_REWRITES: 'static.example.com,other.example.com=10.0.0.5'
```

Format: comma-separated entries of `domain` or `domain=ip`

### Domain Exclusions

Exclude domains from synchronization:

```yaml
environment:
  EXTERNAL_DNS_EXCLUDE_DOMAINS: "auth.example.com,*.internal.*,~^dev-\d+\.example\.com$"
```

Supports three formats:

- **Exact match**: `auth.example.com`
- **Wildcard**: `*.internal.*`, `dev-*`
- **Regex** (prefix with `~`): `~^staging-\d+\.example\.com$`

### Zone Classification

Zone classification allows you to control which domains are synced to your local DNS provider vs forwarded to upstream DNS servers (like Cloudflare or Google).

```yaml
environment:
  EXTERNAL_DNS_DEFAULT_ZONE: 'internal' # Default zone for routers (internal or external)
```

**Zone Types:**

- `internal`: Create DNS rewrites in local DNS provider (e.g., AdGuard)
- `external`: Skip local DNS by default - queries are forwarded to upstream DNS servers

**Zone Detection Priority (first match wins):**

1. **Router name suffix**: `-internal` or `-external` in the router name
2. **Default zone**: Falls back to `EXTERNAL_DNS_DEFAULT_ZONE`

#### Example: Multiple Routers per Service

A single service can define both internal and external routers:

```yaml
services:
  myapp:
    labels:
      traefik.enable: true
      # Internal router: synced to local DNS (resolves to internal IP)
      traefik.http.routers.myapp-internal.rule: Host(`myapp.local.example.com`)
      traefik.http.routers.myapp-internal.service: myapp
      # External router: NOT synced to local DNS (resolves via upstream DNS)
      traefik.http.routers.myapp-external.rule: Host(`myapp.example.com`)
      traefik.http.routers.myapp-external.service: myapp
      traefik.http.services.myapp.loadbalancer.server.port: 8080
```

In this setup:

- `myapp.local.example.com` → local DNS rewrite pointing to internal Traefik IP
- `myapp.example.com` → no local rewrite, resolved via upstream DNS (Cloudflare/Google)

#### Publishing External Routes with a Public IP

External routers are skipped by default to preserve existing local-DNS behavior. To publish external/public routers to configured DNS providers, set `public_target_ip` on the Traefik source that owns those routers:

```yaml
sources:
  - name: primary
    type: traefik
    url: 'https://traefik.example.com'
    target_ip: '192.168.1.10'
    public_target_ip: '${PUBLIC_TARGET_IP}'
```

With this configuration, internal routers from the source still use `target_ip`, while external routers from the same source are written with `public_target_ip`. The public value is never inferred from `target_ip`; operators must configure it explicitly.

### Post-Sync Webhooks

Trigger external services (like [adguardhome-sync](https://github.com/bakito/adguardhome-sync)) after DNS record changes to synchronize across replicas.

```yaml
environment:
  EXTERNAL_DNS_WEBHOOK_URL: 'http://adguardhome-sync:8080/api/v1/sync'
  EXTERNAL_DNS_WEBHOOK_USERNAME: 'admin' # Optional: HTTP Basic Auth
  EXTERNAL_DNS_WEBHOOK_PASSWORD: 'secret' # Optional: HTTP Basic Auth
  EXTERNAL_DNS_WEBHOOK_METHOD: 'POST' # HTTP method (default: POST)
  EXTERNAL_DNS_WEBHOOK_TIMEOUT: '30' # Request timeout in seconds (default: 30)
  EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES: 'true' # Only trigger when records change (default: true)
```

Or via YAML config file:

```yaml
webhook:
  url: 'http://adguardhome-sync:8080/api/v1/sync'
  username: 'admin'
  password: 'secret'
  method: 'POST'
  timeout: 30
  only_on_changes: true
```

**Use Case: AdGuard Home Replicas**

When running multiple AdGuard Home instances behind a load balancer, external-dns writes to the primary instance. The webhook triggers adguardhome-sync to propagate changes to all replicas, ensuring consistent DNS resolution regardless of which replica handles the query.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f433/512.gif" width="32" height="32" alt="whale"> Docker Compose Example

```yaml
services:
  external-dns:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: external-dns
    cap_drop:
      - ALL
    depends_on:
      adguard:
        condition: service_started
    environment:
      # Provider selection
      DNS_PROVIDER: adguard
      PROXY_PROVIDER: traefik
      # AdGuard configuration
      ADGUARD_URL: 'http://adguard'
      ADGUARD_USERNAME: 'admin'
      ADGUARD_PASSWORD: '${ADGUARD_ADMIN_PASSWORD}'
      # Traefik configuration
      TRAEFIK_INSTANCES: |
        [
          {"name": "main", "url": "http://traefik:8080", "target_ip": "192.168.1.2"}
        ]
      # Optional exclusions
      EXTERNAL_DNS_EXCLUDE_DOMAINS: 'auth.example.com'
      # Zone classification (internal vs external)
      EXTERNAL_DNS_DEFAULT_ZONE: 'internal'
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    volumes:
      - ./data:/data
```

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/2728/512.gif" width="32" height="32" alt="sparkles"> Environment Variables Reference

Complete list of configuration options:

| Variable                               | Default               | Description                                                                                           |
| -------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------- |
| `DNS_PROVIDER`                         | `adguard`             | DNS provider type                                                                                     |
| `PROXY_PROVIDER`                       | `traefik`             | Reverse proxy type                                                                                    |
| `ADGUARD_URL`                          | `http://adguard`      | AdGuard Home API URL                                                                                  |
| `ADGUARD_USERNAME`                     | (empty)               | AdGuard admin username                                                                                |
| `ADGUARD_PASSWORD`                     | (empty)               | AdGuard admin password                                                                                |
| `TECHNITIUM_URL`                       | (empty)               | Technitium DNS Server API URL                                                                         |
| `TECHNITIUM_API_TOKEN`                 | (empty)               | Technitium API token sent as a bearer token                                                           |
| `TECHNITIUM_ZONES`                     | (empty)               | Comma-separated authoritative zones for Technitium                                                    |
| `CONFIG_PATH`                          | `/config/config.yaml` | Path to config file                                                                                   |
| `TRAEFIK_INSTANCES`                    | (empty)               | JSON array of Traefik instances used when YAML `sources` are not configured                           |
| `TRAEFIK_URL`                          | `http://traefik:8080` | Single-instance Traefik URL (legacy)                                                                  |
| `TRAEFIK_TARGET_IP`                    | (empty)               | Single-instance target IP (legacy, falls back to `INTERNAL_IP`)                                       |
| `INTERNAL_IP`                          | (empty)               | Fallback IP for `TRAEFIK_TARGET_IP`                                                                   |
| `PUBLIC_TARGET_IP`                     | (empty)               | Example value for YAML `sources[].public_target_ip`; not read directly by legacy single-instance mode |
| `SYNC_MODE`                            | `watch`               | `once` or `watch`                                                                                     |
| `POLL_INTERVAL_SECONDS`                | `60`                  | Polling interval in watch mode                                                                        |
| `LOG_LEVEL`                            | `INFO`                | `DEBUG`, `INFO`, `WARNING`, `ERROR`                                                                   |
| `STATE_PATH`                           | `/data/state.json`    | State file location                                                                                   |
| `EXTERNAL_DNS_STATIC_REWRITES`         | (empty)               | Static DNS rewrites                                                                                   |
| `EXTERNAL_DNS_EXCLUDE_DOMAINS`         | (empty)               | Domain exclusion patterns                                                                             |
| `EXTERNAL_DNS_DEFAULT_ZONE`            | `internal`            | Default zone (`internal`/`external`)                                                                  |
| `EXTERNAL_DNS_WEBHOOK_URL`             | (empty)               | Webhook URL to call after sync                                                                        |
| `EXTERNAL_DNS_WEBHOOK_USERNAME`        | (empty)               | Webhook HTTP Basic Auth username                                                                      |
| `EXTERNAL_DNS_WEBHOOK_PASSWORD`        | (empty)               | Webhook HTTP Basic Auth password                                                                      |
| `EXTERNAL_DNS_WEBHOOK_METHOD`          | `POST`                | Webhook HTTP method                                                                                   |
| `EXTERNAL_DNS_WEBHOOK_TIMEOUT`         | `30`                  | Webhook request timeout (seconds)                                                                     |
| `EXTERNAL_DNS_WEBHOOK_ONLY_ON_CHANGES` | `true`                | Only call webhook when DNS records change                                                             |

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f525/512.gif" width="32" height="32" alt="fire"> Development

Prereqs: Python 3.12+

```bash
make venv
make install
make pre-commit
```

Common commands:

```bash
make lint          # Run linter
make format        # Format code
make test          # Run unit tests
make build         # Build Python package
make run           # Run external-dns locally (requires .env)
make stack         # Start full local test stack
```

### <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f3af/512.gif" width="24" height="24" alt="target"> Local Test Stack

`make stack` (or `make docker`) builds the local image and starts a full test stack (Traefik + AdGuard Home + whoami + external-dns).

- AdGuard UI/API: <http://localhost:3000> (default credentials: `admin` / `password`)
- Traefik dashboard/API: <http://localhost:8080>

> **Production Note:** The `docker-compose.yaml` defaults are configured for local development. For production deployments, copy `.env.example` to `.env` and update service URLs, credentials, IP addresses, and ports to match your environment. See `.env.example` for detailed documentation of all available configuration options.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f389/512.gif" width="32" height="32" alt="party popper"> Releases

- Commit messages are validated locally via `pre-commit` (commit-msg hook) and in CI.
- Versioning and releases are automated on `main` via GitHub Actions using semantic versioning derived from Conventional Commits.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f916/512.gif" width="32" height="32" alt="robot"> GitHub Actions

- CI: lint + format check + tests + Python package build
- Docker: builds (and pushes on `main`/tags) to GitHub Container Registry (GHCR)
- Release: bumps version, updates `CHANGELOG.md`, tags `vX.Y.Z`, and creates a GitHub release

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f4ab/512.gif" width="32" height="32" alt="dizzy"> How It Works

1. **Discovery**: Polls the reverse proxy API to discover configured routes/hostnames
2. **Reconciliation**: Compares discovered hostnames against current DNS records
3. **Synchronization**: Creates, updates, or deletes DNS records to match the desired state
4. **Webhook**: Triggers configured webhook (e.g., adguardhome-sync) when records change
5. **State Management**: Maintains state to handle multi-instance deployments and graceful cleanup

### Multi-Instance Behavior

When multiple reverse proxy instances serve the same hostname:

- The first instance (in configuration order) takes precedence
- Conflicts are logged as warnings
- Records are only removed when confirmed absent from all instances

### Safe Deletion

Records are only deleted when:

- The hostname is confirmed absent from a successfully polled instance
- If an instance is unreachable, its records are preserved until the next successful poll

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f31f/512.gif" width="32" height="32" alt="glowing star"> Adding New Providers

The codebase uses an abstract provider pattern. To add a new provider:

1. **DNS Provider**: Implement the `DNSProvider` abstract class
2. **Reverse Proxy Provider**: Implement the `ReverseProxyProvider` abstract class
3. Add the provider to the factory function in the provider registry section
4. Update this README with the new provider documentation

See the existing `AdGuardDNSProvider` and `TraefikProxyProvider` implementations as examples.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/2753/512.gif" width="32" height="32" alt="question"> Troubleshooting

### Enable Debug Logging

```yaml
environment:
  LOG_LEVEL: DEBUG
```

### Common Issues

- **"Cannot connect to DNS provider"**: Verify the URL and credentials
- **"Proxy instance unreachable"**: Check network connectivity and API endpoint
- **Records not updating**: Ensure `POLL_INTERVAL_SECONDS` is reasonable and check for exclusion patterns

### Retry Behavior

Transient network errors are automatically retried with exponential backoff (up to 3 attempts). If errors persist, check your network connectivity and provider status.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f64f/512.gif" width="32" height="32" alt="thanks"> Kudos

[Kubernetes external-dns](https://github.com/kubernetes-sigs/external-dns)
[Get Shit Done](https://github.com/glittercowboy/get-shit-done) (GSD)

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f3c1/512.gif" width="32" height="32" alt="checkered flag"> License

MIT License. See `LICENSE` file for details.
