# External Integrations

**Analysis Date:** 2025-12-28

## APIs & External Services

**DNS Provider - AdGuard Home:**
- AdGuard Home - DNS record management (rewrites)
  - SDK/Client: requests library via HTTP API
  - Auth: HTTPBasicAuth (username/password) - `ADGUARD_USERNAME`, `ADGUARD_PASSWORD` env vars
  - Base URL: `ADGUARD_URL` env var
  - Endpoints used:
    - `/control/status` - Test connection
    - `/control/rewrite/list` - Get DNS records
    - `/control/rewrite/add` - Add DNS record
    - `/control/rewrite/delete` - Delete DNS record

**Reverse Proxy Provider - Traefik:**
- Traefik - Route discovery from HTTP routers
  - SDK/Client: requests library via HTTP API
  - Auth: Optional HTTPBasicAuth per instance
  - Configuration methods (priority order):
    1. YAML config file: `TRAEFIK_CONFIG_PATH` env var
    2. JSON environment variable: `TRAEFIK_INSTANCES` env var
    3. Legacy single-instance: `TRAEFIK_URL` + `TRAEFIK_TARGET_IP` env vars
  - Endpoints used:
    - `/api/http/routers` - Get router list with rules

## Data Storage

**State Storage:**
- JSON file - Persistent state tracking
  - Location: `STATE_PATH` env var (default: `/data/state.json`)
  - Contents: Domain ownership, instance status, version
  - Pattern: Atomic writes via temp file + rename

**Configuration Storage:**
- YAML files - Multi-instance Traefik configuration
  - Location: `TRAEFIK_CONFIG_PATH` env var (default: `/config/traefik-instances.yaml`)
  - Auto-reload: Monitors file mtime changes in watch mode

**File Storage:**
- Not applicable (no user uploads or file storage)

**Caching:**
- Not applicable (no Redis or other caching layer)

## Authentication & Identity

**Auth Provider:**
- Not applicable (service-to-service authentication only)

**Service Authentication:**
- HTTP Basic Auth - For AdGuard Home and Traefik APIs
  - Credentials: Environment variables per service
  - TLS verification: Configurable via `VERIFY_TLS` (per Traefik instance)

## Monitoring & Observability

**Error Tracking:**
- Not applicable (no external error tracking service)
- Errors logged to stdout via Python logging

**Analytics:**
- Not applicable

**Logs:**
- Python logging to stdout/stderr
- Log level configurable via `LOG_LEVEL` env var (DEBUG, INFO, WARNING, ERROR)
- Format: Standard Python logging format

## CI/CD & Deployment

**Hosting:**
- Docker container - Published to GHCR
  - Image: `ghcr.io/{owner}/external-dns`
  - Tags: branch refs, version tags, commit SHAs
  - Multi-arch: Built via docker/buildx

**CI Pipeline:**
- GitHub Actions - `.github/workflows/`
  - `ci.yml` - Lint, format check, test, build on PRs and main
  - `docker.yml` - Multi-arch Docker build and push
  - `release.yml` - Semantic versioning and releases
- Secrets: `GH_TOKEN` for releases, `GITHUB_TOKEN` for GHCR

**Dependency Management:**
- Renovate Bot - Automated updates (`renovate.json`)
  - Managers: dockerfile, docker-compose, github-actions, pep621
  - Grouping: Docker images, GitHub Actions, Python dependencies

## Environment Configuration

**Development:**
- Required env vars: `DNS_PROVIDER`, `PROXY_PROVIDER`, provider-specific vars
- Secrets location: `.env.local` (gitignored) or shell environment
- Mock/stub services: Local Docker stack via `docker-compose.yaml`

**Staging:**
- Not explicitly defined (same configuration as production)

**Production:**
- Secrets management: Environment variables via container orchestrator
- Config persistence: Mount `/config` and `/data` volumes

## Webhooks & Callbacks

**Incoming:**
- None

**Outgoing:**
- None

---

*Integration audit: 2025-12-28*
*Update when adding/removing external services*
