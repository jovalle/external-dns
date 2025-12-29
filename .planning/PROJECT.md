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
- ✓ Comprehensive test coverage for sync logic (121 tests) — v1.0.0
- ✓ Comprehensive test coverage for provider implementations — v1.0.0
- ✓ Error handling hardening (JSON parsing, network failures) — v1.0.0
- ✓ Retry logic with exponential backoff for transient failures — v1.0.0
- ✓ Complete documentation (README, configuration reference) — v1.0.0
- ✓ Changelog maintenance and version hygiene — v1.0.0

### Active

<!-- Current scope. Building toward these. -->

(None — v1.0.0 complete)

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- New providers (nginx-proxy-manager, Technitium, Pi-hole, Caddy, etc.) — focus on Traefik-AdGuard until bulletproof
- UI/Dashboard — CLI/daemon only, no web interface
- User-defined record management — external-dns manages only synced records, not user's manual entries
- Advanced DNS features (TTL control, record types beyond rewrites) — keep scope minimal for v1

## Context

Fortune 100 infrastructure context. Multiple Docker hosts running Traefik for auto-provisioned service routing. AdGuard Home provides DNS resolution. The gap: Traefik knows about services, but DNS doesn't update automatically. Manual DNS updates don't scale and are error-prone.

This is a common pairing (Traefik + AdGuard Home) in homelab and small infrastructure contexts. Getting this right means the foundation is solid before expanding to other providers.

**Current state (v1.0.0):** Production-ready daemon with:
- 121 unit tests covering sync logic, providers, and utilities
- Retry logic with exponential backoff for all HTTP calls
- Graceful degradation — watch mode continues through errors
- Complete documentation (all 18 env vars, changelog, contributing guide)
- 4,332 lines of Python

**Codebase map:** `.planning/codebase/` contains 7 analysis documents from 2025-12-28.

## Constraints

- **Deployment**: Docker-first — container is the primary interface, CLI is secondary
- **Runtime**: Python 3.12+ — no backporting to older versions
- **Dependencies**: Minimal footprint — requests, PyYAML, stdlib only

## Key Decisions

<!-- Decisions that constrain future work. Add throughout project lifecycle. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single-file monolithic module | Simplifies deployment, no import complexity | ✓ Good — worked well with 1,500 LOC |
| Provider pattern with ABCs | Enables future extensibility without core changes | ✓ Good |
| Environment variable config | Works well in containerized/k8s environments | ✓ Good |
| JSON state file | Simple persistence, human-readable for debugging | ✓ Good — state preserved across restarts |
| Polling-based watch mode | Portable, works without inotify/fsevents | ✓ Good — reliable operation |
| Retry with exponential backoff | Handle transient network failures gracefully | ✓ Good — v1.0.0 |
| Graceful degradation in watch mode | Never crash on recoverable errors | ✓ Good — v1.0.0 |

---
*Last updated: 2025-12-29 after v1.0.0 milestone*
