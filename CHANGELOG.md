# CHANGELOG


## v1.0.1 (2026-01-02)

### Bug Fixes

* fix: simplify semantic-release configuration

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`582cad1`](https://github.com/jovalle/external-dns/commit/582cad1440e41840f33e18f29dce0ecd2e565d15))

* fix: sync ruff versions between pre-commit and dev dependencies

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`17d92cd`](https://github.com/jovalle/external-dns/commit/17d92cde3b0eb32fbe37cf59711395414e8a1f4e))


## v1.0.0 (2026-01-01)

### Breaking

* feat!: v1.0.0 - production-ready release

BREAKING CHANGE: First stable release with complete feature set

Core Features:
- Automatic DNS sync from Traefik to AdGuard Home
- Multi-instance Traefik support with conflict resolution
- Zone classification (internal/external) for selective DNS
- Static DNS rewrites and domain exclusions
- Hot-reload configuration in watch mode

Reliability:
- 127 unit tests covering sync logic, providers, utilities
- Retry logic with exponential backoff
- Graceful degradation - daemon never crashes
- Safe deletion when instances unreachable

DevOps:
- Docker multi-arch builds (amd64, arm64)
- GitHub Actions CI/CD (lint, test, build, release)
- Semantic versioning with automated releases
- Docker Compose stack for local testing
- Integration tests validating full stack

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`c2bf537`](https://github.com/jovalle/external-dns/commit/c2bf5376e1cb828925b3631771dc04cfd1460698))

### Chores

* chore: complete v1.0.0 milestone

- Created MILESTONES.md entry with stats and accomplishments
- Evolved PROJECT.md with validated requirements
- Reorganized ROADMAP.md with milestone archive link
- Created milestone archive: milestones/v1.0.0-ROADMAP.md
- Updated STATE.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`000079f`](https://github.com/jovalle/external-dns/commit/000079f974fcdc54b03e26a22615a95524e636eb))

### Documentation

* docs: initialize external-dns roadmap (3 phases)

Harden working prototype into production-ready daemon

Phases:
1. testing-foundation: Comprehensive test coverage for sync logic and providers
2. error-handling: Retry logic, network failure handling, JSON parsing robustness
3. documentation-release: README accuracy, configuration reference, changelog

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`dcf8ec6`](https://github.com/jovalle/external-dns/commit/dcf8ec6d2ea57ec5a8008a3b1168796a15e1f982))

* docs: initialize external-dns

Sync Traefik routes to AdGuard Home DNS automatically.

Creates PROJECT.md with requirements and constraints.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`1e95077`](https://github.com/jovalle/external-dns/commit/1e95077519f860b0e3e89eb997c3ed53737f6aa0))

* docs: map existing codebase

- STACK.md - Technologies and dependencies
- ARCHITECTURE.md - System design and patterns
- STRUCTURE.md - Directory layout
- CONVENTIONS.md - Code style and patterns
- TESTING.md - Test structure
- INTEGRATIONS.md - External services
- CONCERNS.md - Technical debt and issues

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`12a5221`](https://github.com/jovalle/external-dns/commit/12a5221bafa40ad5b070c20f8e58b9feb237ca5f))

### Features

* feat(03-01): complete documentation with env var reference, changelog, and contributing guide

- Added Environment Variables Reference table to README (18 variables documented)
- Populated CHANGELOG.md with Keep a Changelog format covering Phase 1-2 accomplishments
- Enhanced CONTRIBUTING.md with Testing section and Local Development Stack instructions ([`1c903e7`](https://github.com/jovalle/external-dns/commit/1c903e78e1b68dcd4e5ad2df998570b20ba17f59))

* feat(02-03): graceful degradation and comprehensive error logging

- Improve provider factory error messages with supported providers list
- Add HTTP status codes to all AdGuard provider error messages
- Enhanced sync_once error context with URL and HTTP status codes
- Add watch loop catch-all exception handler for daemon resilience
- Add periodic health check logging every 10 cycles
- Add 6 new tests (2 error message quality + 4 graceful degradation)

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`6cd6dc0`](https://github.com/jovalle/external-dns/commit/6cd6dc0fa7ca657d9841262618d8621b1f19c1b5))

* feat(02-02): retry logic with exponential backoff for transient failures

- Add retry_with_backoff() utility function with configurable params
- Wrap AdGuard test_connection, get_records, add_record, delete_record
- Wrap Traefik get_routes with retry logic
- Add 10 new tests (6 utility + 4 provider retry tests)

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`06a36df`](https://github.com/jovalle/external-dns/commit/06a36df6ea74b82806944e0f73a9e93b252c7a23))

* feat(02-01): JSON parsing robustness for AdGuard and Traefik providers

- Add JSONDecodeError handling to both providers
- Use safe .get() with type validation in AdGuard get_records()
- Validate routers list type in Traefik get_routes()
- Skip malformed records with warning/debug logs instead of crashing
- Add 6 new tests for JSON error scenarios (3 per provider)

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`ceefaeb`](https://github.com/jovalle/external-dns/commit/ceefaeb4d46bf381fce97dfaad6abe5e856d2dc0))

* feat(01-03): 19 utility tests plus integration test documentation

- Expanded test_utils.py: static rewrites, exclude patterns, boolean parsing, config file finding
- Enhanced test_docker_stack.py with docstrings, helper functions, scenario docs
- Phase 1: Testing Foundation complete (99 tests passing) ([`e74b231`](https://github.com/jovalle/external-dns/commit/e74b2310725ff0e13cf37da578055a4a9c376fa1))

* feat(01-02): 51 unit tests for AdGuard, Traefik, and StateStore providers

- AdGuard Home provider: 14 tests covering connection, CRUD, authentication
- Traefik provider: 26 tests covering instance loading, route discovery, filtering, zones
- StateStore: 11 tests covering load/save operations, atomic writes, default state ([`9dad6d5`](https://github.com/jovalle/external-dns/commit/9dad6d503dc9686b51d07066f5d6f180d66e3653))

* feat(01-01): 20 unit tests for ExternalDNSSyncer reconciliation logic

- Mock DNS/proxy providers with in-memory storage and call tracking
- Tests for CRUD operations, multi-instance scenarios, domain filtering
- Zone handling, static rewrites, and edge cases covered
- All tests pass with existing make lint/test pipeline

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com> ([`d69d90e`](https://github.com/jovalle/external-dns/commit/d69d90e5420b74fc53549ec70e2c91f3672ff20f))

### Unknown

* Initial commit ([`a627eac`](https://github.com/jovalle/external-dns/commit/a627eaccbb230057a9e33114176220af16dfd9cb))
