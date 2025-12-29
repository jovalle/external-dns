# Roadmap: external-dns

## Overview

Harden the working external-dns prototype into a production-ready daemon. Focus on test coverage for sync logic correctness, robust error handling for real-world network conditions, and complete documentation for users and contributors.

## Domain Expertise

None

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

- [ ] **Phase 1: Testing Foundation** - Comprehensive test coverage for sync logic and provider implementations
- [ ] **Phase 2: Error Handling Hardening** - Retry logic, network failure handling, JSON parsing robustness
- [ ] **Phase 3: Documentation & Release** - README accuracy, configuration reference, changelog maintenance

## Phase Details

### Phase 1: Testing Foundation
**Goal**: Achieve comprehensive test coverage for reconciliation logic and both providers (Traefik, AdGuard Home)
**Depends on**: Nothing (first phase)
**Research**: Unlikely (internal Python testing patterns)
**Plans**: TBD

Key areas:
- Sync reconciliation logic (add/update/delete, edge cases, ownership tracking)
- Traefik provider (multi-instance, router filtering, zone detection)
- AdGuard Home provider (CRUD operations, API interactions)
- Integration tests for end-to-end sync scenarios

### Phase 2: Error Handling Hardening
**Goal**: Make the daemon resilient to transient failures and malformed data
**Depends on**: Phase 1 (tests enable confident refactoring)
**Research**: Unlikely (established Python error handling patterns)
**Plans**: TBD

Key areas:
- JSON parsing errors (malformed responses from Traefik/AdGuard APIs)
- Network failures (connection timeouts, unreachable hosts, HTTP errors)
- Retry logic with exponential backoff for transient failures
- Graceful degradation when providers are temporarily unavailable

### Phase 3: Documentation & Release
**Goal**: Complete documentation and establish version hygiene for ongoing maintenance
**Depends on**: Phase 2 (document hardened behavior)
**Research**: Unlikely (internal documentation work)
**Plans**: TBD

Key areas:
- README accuracy (ensure docs match current implementation)
- Configuration reference (all environment variables documented)
- Changelog maintenance (semantic versioning, release notes)
- Contributing guide updates

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Testing Foundation | 0/TBD | Not started | - |
| 2. Error Handling Hardening | 0/TBD | Not started | - |
| 3. Documentation & Release | 0/TBD | Not started | - |
