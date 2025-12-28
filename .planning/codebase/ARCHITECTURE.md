# Architecture

**Analysis Date:** 2025-12-28

## Pattern Overview

**Overall:** Layered CLI Application with Provider Pattern

**Key Characteristics:**
- Single executable with watch/once sync modes
- Provider-based extensibility (DNS and reverse proxy adapters)
- File-based state persistence
- Polling-based configuration reload

## Layers

**Configuration Layer:**
- Purpose: Load and validate configuration from environment variables
- Contains: Environment variable parsing, config validation
- Location: `src/external_dns/cli.py` lines 163-191
- Depends on: Python stdlib (os, logging)
- Used by: Main entry point, provider factories

**Data Model Layer:**
- Purpose: Define core data structures
- Contains: `DNSZone` enum, `DNSRecord`, `ProxyRoute`, `ProxyInstance` dataclasses
- Location: `src/external_dns/cli.py` lines 208-260
- Depends on: Python stdlib (dataclasses, enum)
- Used by: All layers

**Provider Interface Layer:**
- Purpose: Abstract interfaces for extensibility
- Contains: `DNSProvider` (ABC), `ReverseProxyProvider` (ABC)
- Location: `src/external_dns/cli.py` lines 267-384
- Depends on: Data model layer
- Used by: Provider implementations, core syncer

**Provider Implementation Layer:**
- Purpose: Concrete implementations for specific services
- Contains: `AdGuardDNSProvider`, `TraefikProxyProvider`
- Location: `src/external_dns/cli.py` lines 303-634
- Depends on: Provider interfaces, requests library
- Used by: Core syncer via factory functions

**Utility Layer:**
- Purpose: Shared helpers and parsing functions
- Contains: File watching, pattern parsing, domain exclusion logic
- Location: `src/external_dns/cli.py` lines 121-156, 673-745
- Depends on: Python stdlib (re, fnmatch, pathlib)
- Used by: Provider implementations, core syncer

**State Management Layer:**
- Purpose: Persist domain ownership and instance status
- Contains: `StateStore` class
- Location: `src/external_dns/cli.py` lines 753-771
- Depends on: Python stdlib (json, pathlib)
- Used by: Core syncer

**Core Syncer Layer:**
- Purpose: Orchestrate DNS synchronization logic
- Contains: `ExternalDNSSyncer` class
- Location: `src/external_dns/cli.py` lines 778-1053
- Depends on: All other layers
- Used by: Main entry point

## Data Flow

**CLI Command Execution (once mode):**

1. User runs `external-dns` command
2. `main()` validates configuration via `validate_config()`
3. Provider factories create DNS and proxy provider instances
4. DNS provider connection test via `test_connection()`
5. `ExternalDNSSyncer` created with providers and state store
6. `syncer.sync_once()` executes:
   - Load state from JSON file
   - Get proxy instances and routes from reverse proxy provider
   - Filter routes (exclusions, zones)
   - Reconcile with current DNS records
   - Add/update/delete DNS records as needed
   - Save state to JSON file
7. Process exits with status code

**Watch Mode (continuous):**

1. Same initialization as once mode
2. Infinite loop every `POLL_INTERVAL_SECONDS`:
   - Execute `sync_once()`
   - Check config file modification times
   - If changed: reload proxy provider, trigger immediate re-sync
   - Sleep for poll interval

**State Management:**
- File-based: JSON state persisted at `STATE_PATH`
- Tracks domain ownership per proxy instance
- Enables cleanup of orphaned records when instances removed
- Atomic writes via temp file + rename

## Key Abstractions

**DNSProvider:**
- Purpose: Abstract DNS operations
- Examples: `AdGuardDNSProvider`
- Pattern: Abstract base class with `@abstractmethod`
- Methods: `test_connection()`, `get_records()`, `add_record()`, `delete_record()`, `update_record()`

**ReverseProxyProvider:**
- Purpose: Abstract reverse proxy route discovery
- Examples: `TraefikProxyProvider`
- Pattern: Abstract base class with `@abstractmethod`
- Methods: `get_instances()`, `get_routes()`

**ExternalDNSSyncer:**
- Purpose: Core synchronization orchestrator
- Pattern: Coordinator that uses providers and state store
- Key method: `sync_once()` - single reconciliation cycle

**StateStore:**
- Purpose: Persistent state management
- Pattern: Simple JSON file storage with atomic writes
- Methods: `load()`, `save()`

**Provider Factories:**
- Purpose: Create configured provider instances
- Pattern: Factory functions based on env vars
- Functions: `create_dns_provider()`, `create_proxy_provider()`

## Entry Points

**CLI Entry (`main()`):**
- Location: `src/external_dns/cli.py` line 1097
- Triggers: `external-dns` console script via setuptools
- Responsibilities: Validate config, create providers, run sync loop

**Wrapper Script:**
- Location: `external-dns.py` (project root)
- Triggers: Direct Python execution for development
- Responsibilities: Add `src/` to path, import and call `main()`

## Error Handling

**Strategy:** Catch at boundaries, log and continue or exit

**Patterns:**
- Provider methods return `bool` for success/failure (add, delete, update)
- Provider methods return empty list on connection error (get_records, get_routes)
- Validation errors cause early exit with message
- Sync errors logged, sync continues for other instances
- Watch mode continues running after sync errors

## Cross-Cutting Concerns

**Logging:**
- Python stdlib logging module
- Logger instance per module: `logger = logging.getLogger(__name__)`
- Levels: DEBUG, INFO, WARNING, ERROR
- Configured via `LOG_LEVEL` env var

**Validation:**
- Environment variable validation in `validate_config()`
- Fail fast on missing required configuration
- IP address and domain format validation (limited)

**Configuration:**
- All settings via environment variables
- Multi-instance config via YAML or JSON
- Config file hot-reload in watch mode

---

*Architecture analysis: 2025-12-28*
*Update when major patterns change*
