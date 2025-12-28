# Codebase Structure

**Analysis Date:** 2025-12-28

## Directory Layout

```
external-dns/
├── src/
│   └── external_dns/       # Main Python package
│       ├── __init__.py     # Package version
│       └── cli.py          # All application code (monolithic)
├── tests/                  # Test suite
│   ├── test_utils.py       # Unit tests for utility functions
│   ├── test_config_watch.py # Unit tests for file watching
│   └── integration/        # Integration tests
│       └── test_docker_stack.py
├── docker/                 # Docker support files
│   ├── local/              # Local dev stack volumes
│   │   ├── adguard/        # AdGuard config/data
│   │   └── external-dns/   # App config/data
│   ├── config/             # Config templates
│   └── data/               # Runtime data
├── .github/
│   └── workflows/          # CI/CD pipelines
│       ├── ci.yml          # Lint, test, build
│       ├── docker.yml      # Multi-arch Docker build
│       └── release.yml     # Semantic release
├── external-dns.py         # Development wrapper script
├── Dockerfile              # Container image definition
├── docker-compose.yaml     # Local dev stack
├── docker-compose.integration.yaml # Integration test stack
├── pyproject.toml          # Package config & dependencies
├── Makefile                # Development task runner
└── README.md               # Project documentation
```

## Directory Purposes

**src/external_dns/**
- Purpose: Main application package
- Contains: Single monolithic module (`cli.py`) with all application code
- Key files:
  - `__init__.py` - Package version string
  - `cli.py` - All classes, functions, entry point (1,235 lines)
- Subdirectories: None

**tests/**
- Purpose: Test suite (unit and integration)
- Contains: pytest test files
- Key files:
  - `test_utils.py` - Tests for parsing utilities
  - `test_config_watch.py` - Tests for config file watching
- Subdirectories: `integration/` for Docker-based tests

**docker/**
- Purpose: Docker and local development support
- Contains: Volume mount directories, config templates
- Key files:
  - `config/traefik.yaml.template` - Traefik config example
  - `local/` - Local dev stack persistent data
- Subdirectories: `local/`, `config/`, `data/`

**.github/workflows/**
- Purpose: CI/CD automation
- Contains: GitHub Actions workflow definitions
- Key files:
  - `ci.yml` - Main CI pipeline (lint, test, build)
  - `docker.yml` - Docker image builds
  - `release.yml` - Automated releases

## Key File Locations

**Entry Points:**
- `src/external_dns/cli.py:main()` - Main entry point (line 1097)
- `external-dns.py` - Development wrapper script

**Configuration:**
- `pyproject.toml` - Package metadata, dependencies, tool config
- `.editorconfig` - Editor formatting rules
- `.pre-commit-config.yaml` - Git hooks
- `.env.example` - Environment variable template

**Core Logic:**
- `src/external_dns/cli.py` - All application code:
  - Lines 163-191: Configuration loading
  - Lines 267-384: Provider interfaces (ABC)
  - Lines 303-359: AdGuard DNS provider
  - Lines 386-634: Traefik proxy provider
  - Lines 753-771: State management
  - Lines 778-1053: Core syncer logic

**Testing:**
- `tests/test_utils.py` - Utility function tests
- `tests/test_config_watch.py` - File watching tests
- `tests/integration/test_docker_stack.py` - End-to-end tests

**Documentation:**
- `README.md` - User documentation, configuration reference
- `CONTRIBUTING.md` - Development guidelines
- `CHANGELOG.md` - Version history

## Naming Conventions

**Files:**
- snake_case for Python modules: `cli.py`, `test_utils.py`
- kebab-case for config files: `docker-compose.yaml`
- UPPERCASE for important docs: `README.md`, `CHANGELOG.md`
- Dotfiles for tool config: `.editorconfig`, `.pre-commit-config.yaml`

**Directories:**
- snake_case: `external_dns/`, `integration/`
- kebab-case for some: `docker/local/`
- Plural for collections: `tests/`, `workflows/`

**Special Patterns:**
- `test_*.py` - pytest test files
- `*.template` - Config templates (excluded from auto-loading)
- `__init__.py` - Python package marker

## Where to Add New Code

**New DNS Provider:**
- Implementation: Add class in `src/external_dns/cli.py` after `AdGuardDNSProvider`
- Registration: Update `create_dns_provider()` factory function
- Tests: Add `tests/test_{provider_name}.py`

**New Reverse Proxy Provider:**
- Implementation: Add class in `src/external_dns/cli.py` after `TraefikProxyProvider`
- Registration: Update `create_proxy_provider()` factory function
- Tests: Add `tests/test_{provider_name}.py`

**New Utility Function:**
- Implementation: Add in `src/external_dns/cli.py` utility section (lines 121-156 or 673-745)
- Tests: Add to `tests/test_utils.py`

**New Configuration Option:**
- Definition: Add env var in configuration section (lines 163-191)
- Validation: Update `validate_config()` function
- Documentation: Update `README.md` and `cli.py` docstring

## Special Directories

**docker/local/**
- Purpose: Persistent volumes for local development stack
- Source: Created by docker-compose on first run
- Committed: Structure committed, contents gitignored

**docker/data/**
- Purpose: Runtime data for Docker containers
- Source: Created at runtime
- Committed: No (gitignored)

**.venv/**
- Purpose: Python virtual environment
- Source: Created by `make setup`
- Committed: No (gitignored)

---

*Structure analysis: 2025-12-28*
*Update when directory structure changes*
