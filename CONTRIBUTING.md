# Contributing

## Development Setup

Create a venv and install dev dependencies:

```bash
make venv
make install
```

Install git hooks (lint/format + Conventional Commits validation):

```bash
make pre-commit
```

## Quality Checks

- Lint: `make lint`
- Format: `make format`
- Tests: `make test`
- Build: `make build`

## Testing

All changes should include tests. Run the full suite:

```bash
make test
```

Current coverage areas:
- Sync reconciliation logic (ExternalDNSSyncer)
- Provider implementations (AdGuard, Traefik)
- State persistence (StateStore)
- Utility functions (retry logic, domain exclusions)

## Local Development Stack

Run a complete test environment with Traefik + AdGuard Home:

```bash
make docker
```

- AdGuard: http://localhost:3000 (admin/password)
- Traefik: http://localhost:8080

## Commit Messages (Required)

This repo enforces Conventional Commits.

Examples:
- `feat(adguard): support custom rewrite ttl`
- `fix(traefik): handle routers missing rule`
- `docs: update compose example`

Breaking changes:
- Add `!` after the type/scope (e.g., `feat!: change config format`)
- Or include `BREAKING CHANGE:` in the commit body

## Releases

Releases are automated on `main` via GitHub Actions using semantic versioning from commit history.
