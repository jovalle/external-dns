# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-01-02

First production-ready release of external-dns.

### Added
- **Core Features**
  - Automatic DNS synchronization from Traefik reverse proxy to AdGuard Home
  - Multi-instance Traefik support with conflict resolution
  - Zone classification (internal/external) for selective DNS management
  - Static DNS rewrites for always-present records
  - Domain exclusion patterns (exact, wildcard, regex)
  - Hot-reload of configuration in watch mode

- **Reliability**
  - Comprehensive test suite (127 tests covering sync logic, providers, utilities)
  - Retry logic with exponential backoff for transient network failures
  - Graceful degradation in watch mode - daemon never crashes from recoverable errors
  - Safe deletion - records preserved when proxy instances are unreachable
  - Health check logging every 10 sync cycles

- **DevOps**
  - Docker container with multi-arch support (amd64, arm64)
  - GitHub Actions CI/CD pipeline (lint, test, build, release)
  - Semantic versioning with automated releases
  - Pre-commit hooks for commit message validation
  - Docker Compose stack for local development and testing
  - Integration tests validating full stack behavior

- **Documentation**
  - Complete README with configuration reference
  - Environment variable documentation
  - Docker Compose examples
  - Troubleshooting guide

### Fixed
- JSON parsing errors no longer crash sync cycles (malformed responses handled gracefully)
- Provider factory errors now include supported providers list and configuration hints

### Changed
- Enhanced startup logging with DNS/proxy provider details and instance configuration
- Improved error context with URL and status code information
