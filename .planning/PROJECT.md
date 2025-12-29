# external-dns

## What This Is

A Docker-based daemon that synchronizes DNS records from reverse proxy route configurations. When Traefik auto-provisions routes for new services, external-dns propagates those hostnames to AdGuard Home DNS, eliminating manual DNS management across multiple Docker hosts.

## Core Value

Sync correctness: DNS records always match Traefik routes — no orphans, no missing entries, no duplicates.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

- ✓ Traefik provider with multi-instance support — existing
- ✓ Traefik router filtering (name patterns, middleware) — existing
- ✓ Traefik zone detection (internal/external via router suffix) — existing
- ✓ AdGuard Home provider (CRUD for DNS rewrites) — existing
- ✓ Core sync reconciliation logic — existing
- ✓ State persistence for domain ownership tracking — existing
- ✓ Watch mode with polling and config hot-reload — existing
- ✓ Once mode for single sync execution — existing
- ✓ Provider pattern architecture (extensible ABCs) — existing
- ✓ Docker container deployment — existing
- ✓ CI pipeline (lint, format, test, build) — existing
- ✓ Semantic release workflow — existing
- ✓ Domain exclusion patterns (exact, wildcard, regex) — existing
- ✓ Static rewrites support — existing

### Active

<!-- Current scope. Building toward these. -->

- [ ] Comprehensive test coverage for sync logic (reconciliation, edge cases)
- [ ] Comprehensive test coverage for provider implementations
- [ ] Error handling hardening (JSON parsing, network failures, malformed responses)
- [ ] Retry logic with exponential backoff for transient failures
- [ ] Complete documentation (README accuracy, configuration reference)
- [ ] Changelog maintenance and version hygiene

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- New providers (nginx-proxy-manager, Technitium, Pi-hole, Caddy, etc.) — focus on Traefik-AdGuard until bulletproof
- UI/Dashboard — CLI/daemon only, no web interface
- User-defined record management — external-dns manages only synced records, not user's manual entries
- Advanced DNS features (TTL control, record types beyond rewrites) — keep scope minimal for v1

## Context

Fortune 100 infrastructure context. Multiple Docker hosts running Traefik for auto-provisioned service routing. AdGuard Home provides DNS resolution. The gap: Traefik knows about services, but DNS doesn't update automatically. Manual DNS updates don't scale and are error-prone.

This is a common pairing (Traefik + AdGuard Home) in homelab and small infrastructure contexts. Getting this right means the foundation is solid before expanding to other providers.

**Current state:** Working prototype with core functionality. Needs hardening:
- Test coverage is minimal (6 unit tests, 1 integration test for 1,235 lines)
- Some error handling gaps identified in codebase analysis
- Project hygiene incomplete (docs need updates, changelog workflow)

**Codebase map:** `.planning/codebase/` contains 7 analysis documents from 2025-12-28.

## Constraints

- **Deployment**: Docker-first — container is the primary interface, CLI is secondary
- **Runtime**: Python 3.12+ — no backporting to older versions
- **Dependencies**: Minimal footprint — requests, PyYAML, stdlib only

## Key Decisions

<!-- Decisions that constrain future work. Add throughout project lifecycle. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single-file monolithic module | Simplifies deployment, no import complexity | — Pending |
| Provider pattern with ABCs | Enables future extensibility without core changes | ✓ Good |
| Environment variable config | Works well in containerized/k8s environments | ✓ Good |
| JSON state file | Simple persistence, human-readable for debugging | — Pending |
| Polling-based watch mode | Portable, works without inotify/fsevents | — Pending |

---
*Last updated: 2025-12-28 after initialization*
