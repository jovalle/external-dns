# Codebase Concerns

**Analysis Date:** 2025-12-28

## Tech Debt

**Large monolithic module:**
- Issue: All code in single `src/external_dns/cli.py` file (1,235 lines)
- Files: `src/external_dns/cli.py`
- Why: Simplifies deployment, no import complexity
- Impact: Harder to navigate, test in isolation, and maintain as codebase grows
- Fix approach: Consider splitting into `providers/`, `models/`, `sync/` modules when adding more providers

**Duplicate configuration parsing:**
- Issue: YAML and JSON Traefik config parsing has ~30+ lines of duplication
- Files: `src/external_dns/cli.py` lines 421-458 (YAML) and 469-506 (JSON)
- Why: Added incrementally without refactoring
- Impact: Bug fixes need to be applied twice, easy to miss one
- Fix approach: Extract into `_parse_instance_config(config_dict)` helper

**Large methods:**
- Issue: `sync_once()` is 188 lines, `get_instances()` is 100 lines
- Files: `src/external_dns/cli.py` lines 867-1054 (`sync_once`), 414-513 (`get_instances`)
- Why: Evolved organically with feature additions
- Impact: Hard to test individual concerns, cognitive load when reading
- Fix approach: Extract sub-methods: `_reconcile_records()`, `_filter_routes()`, etc.

## Known Bugs

**Unsafe JSON key access in get_records():**
- Symptoms: KeyError exception if AdGuard returns malformed record
- Trigger: AdGuard API returns record missing `domain` or `answer` field
- Files: `src/external_dns/cli.py` line 331
- Workaround: None - will crash sync cycle
- Root cause: Direct dict key access without validation: `r["domain"]`, `r["answer"]`
- Fix: Use `.get("domain")` with validation or try/except

**Unhandled JSONDecodeError in get_routes():**
- Symptoms: Uncaught exception propagates up
- Trigger: Traefik returns invalid JSON response
- Files: `src/external_dns/cli.py` lines 528-530
- Workaround: None - will crash sync cycle
- Root cause: Only `RequestException` caught, not `JSONDecodeError`
- Fix: Add `except (ValueError, json.JSONDecodeError):` clause

## Security Considerations

**TLS verification defaults to True (safe):**
- Risk: Low - correctly defaults to verifying certificates
- Files: `src/external_dns/cli.py` line 525
- Current mitigation: `verify_tls` defaults to `True`, explicit opt-out required
- Recommendations: None - current behavior is correct

**Credentials via environment variables (appropriate):**
- Risk: Low - standard practice for containerized apps
- Files: `src/external_dns/cli.py` lines 163-191
- Current mitigation: No hardcoded credentials, env vars documented
- Recommendations: Document secret management in production deployments

## Performance Bottlenecks

**No retry logic or backoff:**
- Problem: HTTP requests fail immediately with no retry
- Files: `src/external_dns/cli.py` - all request handlers
- Measurement: Single 5-second timeout, then failure
- Cause: No retry mechanism implemented
- Improvement path: Add `tenacity` or manual retry with exponential backoff

**Polling-based config reload:**
- Problem: Config file changes detected via mtime polling
- Files: `src/external_dns/cli.py` lines 1163-1225
- Measurement: Up to `POLL_INTERVAL_SECONDS` delay before detecting changes
- Cause: Design choice for portability (no inotify dependency)
- Improvement path: Add optional inotify/fsevents support for faster reload

## Fragile Areas

**Provider factory functions:**
- Files: `src/external_dns/cli.py` lines 641-666
- Why fragile: String-based provider selection with no fallback
- Common failures: Typo in `DNS_PROVIDER` env var causes unclear error
- Safe modification: Add supported providers list, better error messages
- Test coverage: Not tested

**Zone detection logic:**
- Files: `src/external_dns/cli.py` lines 569-589
- Why fragile: Router name suffix matching is implicit convention
- Common failures: Router names not following `-internal`/`-external` pattern
- Safe modification: Add configuration for explicit zone mapping
- Test coverage: Not tested

## Scaling Limits

**Single-threaded sync:**
- Current capacity: One sync cycle at a time
- Limit: Many Traefik instances or slow DNS provider
- Symptoms at limit: Sync cycles take longer than poll interval
- Scaling path: Add async/parallel instance processing

**In-memory state during sync:**
- Current capacity: All domain ownership tracked in memory
- Limit: Thousands of domains
- Symptoms at limit: Memory pressure, slow dict lookups
- Scaling path: Unlikely to be an issue in practice

## Dependencies at Risk

**PyYAML:**
- Risk: Low - actively maintained, widely used
- Impact: Config parsing
- Migration plan: N/A - stable dependency

**requests:**
- Risk: Low - de facto standard, actively maintained
- Impact: All HTTP communication
- Migration plan: N/A - stable dependency

## Missing Critical Features

**Label-based zone detection:**
- Problem: Documented but not implemented
- Files: `src/external_dns/cli.py` lines 569-589, `README.md`
- Current workaround: Router name suffix convention
- Blocks: Users expecting Docker/Traefik label-based zone configuration
- Implementation complexity: Medium - requires Docker API or Traefik provider enrichment

**Additional DNS providers:**
- Problem: Only AdGuard Home supported
- Files: `README.md` lists Pi-hole, Technitium, CoreDNS as "planned"
- Current workaround: None - must use AdGuard
- Blocks: Users of other DNS solutions
- Implementation complexity: Low-Medium per provider (follow existing pattern)

**Additional proxy providers:**
- Problem: Only Traefik supported
- Files: `README.md` lists Caddy, Nginx Proxy Manager, HAProxy as "planned"
- Current workaround: None - must use Traefik
- Blocks: Users of other reverse proxies
- Implementation complexity: Low-Medium per provider

## Test Coverage Gaps

**Provider implementations:**
- What's not tested: `AdGuardDNSProvider`, `TraefikProxyProvider` methods
- Files: `src/external_dns/cli.py` lines 303-634
- Risk: HTTP interaction bugs, edge cases in parsing
- Priority: High
- Difficulty to test: Medium - need HTTP mocking or live test servers

**Core sync logic:**
- What's not tested: `ExternalDNSSyncer.sync_once()` reconciliation
- Files: `src/external_dns/cli.py` lines 867-1054
- Risk: Incorrect add/delete/update decisions
- Priority: High
- Difficulty to test: Medium - need provider mocks

**Zone detection:**
- What's not tested: `_detect_zone()` with various router patterns
- Files: `src/external_dns/cli.py` lines 569-589
- Risk: Wrong zone classification
- Priority: Medium
- Difficulty to test: Low - pure function

**Error scenarios:**
- What's not tested: HTTP failures, malformed responses, state corruption
- Risk: Unclear failure modes in production
- Priority: Medium
- Difficulty to test: Medium - need controlled failure injection

---

*Concerns audit: 2025-12-28*
*Update as issues are fixed or new ones discovered*
