# Technology Stack

**Analysis Date:** 2025-12-28

## Languages

**Primary:**
- Python 3.12+ - All application code (`src/external_dns/`)

**Secondary:**
- Shell/Bash - Build scripts, CI workflows (`.github/workflows/`)
- YAML - Configuration files (`docker-compose.yaml`, `.pre-commit-config.yaml`)

## Runtime

**Environment:**
- Python 3.12+ (minimum `requires-python = ">=3.12"` in `pyproject.toml`)
- Docker container runtime (python:3.12-slim base image in `Dockerfile`)

**Package Manager:**
- pip with setuptools build backend
- Lockfile: No lockfile (uses declarative dependencies in `pyproject.toml`)

## Frameworks

**Core:**
- None (vanilla Python CLI application)

**Testing:**
- pytest 8.3+ - Unit and integration tests (`tests/`)
- pytest-cov 5.0+ - Coverage tracking

**Build/Dev:**
- setuptools - Package building (`pyproject.toml`)
- python-build - Wheel/sdist creation
- Ruff 0.8+ - Linting and formatting

## Key Dependencies

**Critical:**
- PyYAML >= 6.0.2 - YAML configuration parsing (`pyproject.toml`)
- requests >= 2.32.3 - HTTP client for API calls (`pyproject.toml`)

**Infrastructure:**
- Python stdlib: `logging`, `json`, `os`, `pathlib`, `dataclasses`, `abc`, `enum`
- requests.auth.HTTPBasicAuth - Authentication for AdGuard/Traefik APIs

## Configuration

**Environment:**
- Primary configuration via environment variables
- Key env vars: `DNS_PROVIDER`, `PROXY_PROVIDER`, `ADGUARD_URL`, `ADGUARD_USERNAME`, `ADGUARD_PASSWORD`, `TRAEFIK_CONFIG_PATH`, `TRAEFIK_INSTANCES`, `SYNC_MODE`, `POLL_INTERVAL_SECONDS`, `LOG_LEVEL`, `STATE_PATH`
- Example config: `.env.example`

**Build:**
- `pyproject.toml` - Package metadata, dependencies, tool configuration
- `.editorconfig` - Editor formatting rules
- `.pre-commit-config.yaml` - Git hooks configuration

## Platform Requirements

**Development:**
- macOS/Linux/Windows (any platform with Python 3.12+)
- Docker for local testing stack (`docker-compose.yaml`)
- Make for task automation (`Makefile`)

**Production:**
- Docker container (python:3.12-slim base)
- Published to GitHub Container Registry (GHCR)
- Runs as non-root user (UID 65532)
- Volumes: `/config` (YAML configs), `/data` (state file)

---

*Stack analysis: 2025-12-28*
*Update after major dependency changes*
