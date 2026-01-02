# external-dns

Universal DNS synchronization service that syncs reverse proxy routes to DNS providers, inspired by [Kubernetes external-dns](https://github.com/kubernetes-sigs/external-dns).

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f4a1/512.gif" width="32" height="32" alt="light bulb"> Overview

external-dns automatically discovers hostnames from your reverse proxy configuration and creates corresponding DNS records in your DNS provider. When routes are added, modified, or removed from the reverse proxy, DNS records are automatically synchronized.

## <img src="https://fonts.gstatic.com/s/e/notoemoji/latest/1f680/512.gif" width="32" height="32" alt="rocket"> Supported Providers

### DNS Providers

| Provider     | Status    | Environment Prefix |
| ------------ | --------- | ------------------ |
| AdGuard Home | Supported | `ADGUARD_`         |
| Pi-hole      | Planned   | -                  |
| Technitium   | Planned   | -                  |
| CoreDNS      | Planned   | -                  |

### Reverse Proxy Providers

| Provider            | Status    | Environment Prefix |
| ------------------- | --------- | ------------------ |
| Traefik             | Supported | `TRAEFIK_`         |
| Caddy               | Planned   | -                  |
| Nginx Proxy Manager | Planned   | -                  |

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
- `target_ip` (required): IP address to use in DNS records
- `verify_tls` (optional): Verify TLS certificates (default: true)
- `username` (optional): Basic auth username
- `password` (optional): Basic auth password

**Single-instance configuration (legacy):**

```yaml
environment:
  TRAEFIK_URL: 'http://traefik:8080'
  TRAEFIK_TARGET_IP: '192.168.1.10'
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
  EXTERNAL_DNS_ZONE_LABEL: 'external-dns.zone' # Custom label name (optional)
```

**Zone Types:**

- `internal`: Create DNS rewrites in local DNS provider (e.g., AdGuard)
- `external`: Skip local DNS - queries are forwarded to upstream DNS servers

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

| Variable                       | Default                | Description                                                     |
| ------------------------------ | ---------------------- | --------------------------------------------------------------- |
| `DNS_PROVIDER`                 | `adguard`              | DNS provider type                                               |
| `PROXY_PROVIDER`               | `traefik`              | Reverse proxy type                                              |
| `ADGUARD_URL`                  | `http://adguard`       | AdGuard Home API URL                                            |
| `ADGUARD_USERNAME`             | (empty)                | AdGuard admin username                                          |
| `ADGUARD_PASSWORD`             | (empty)                | AdGuard admin password                                          |
| `CONFIG_PATH`                  | `/config/config.yaml`  | Path to config file                                             |
| `TRAEFIK_INSTANCES`            | (empty)                | JSON array of Traefik instances (overrides config file)         |
| `TRAEFIK_URL`                  | `http://traefik:8080`  | Single-instance Traefik URL (legacy)                            |
| `TRAEFIK_TARGET_IP`            | (empty)                | Single-instance target IP (legacy, falls back to `INTERNAL_IP`) |
| `INTERNAL_IP`                  | (empty)                | Fallback IP for `TRAEFIK_TARGET_IP`                             |
| `SYNC_MODE`                    | `watch`                | `once` or `watch`                                               |
| `POLL_INTERVAL_SECONDS`        | `60`                   | Polling interval in watch mode                                  |
| `LOG_LEVEL`                    | `INFO`                 | `DEBUG`, `INFO`, `WARNING`, `ERROR`                             |
| `STATE_PATH`                   | `/data/state.json`     | State file location                                             |
| `EXTERNAL_DNS_STATIC_REWRITES` | (empty)                | Static DNS rewrites                                             |
| `EXTERNAL_DNS_EXCLUDE_DOMAINS` | (empty)                | Domain exclusion patterns                                       |
| `EXTERNAL_DNS_DEFAULT_ZONE`    | `internal`             | Default zone (`internal`/`external`)                            |
| `EXTERNAL_DNS_ZONE_LABEL`      | `external-dns.zone`    | Custom zone label name                                          |

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
4. **State Management**: Maintains state to handle multi-instance deployments and graceful cleanup

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
