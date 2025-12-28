# Coding Conventions

**Analysis Date:** 2025-12-28

## Naming Patterns

**Files:**
- snake_case for Python modules: `cli.py`, `test_utils.py`
- `test_` prefix for test files: `test_utils.py`, `test_config_watch.py`
- `*.template` suffix for config templates (excluded from auto-loading)

**Functions:**
- snake_case for all functions: `get_config_file_mtime()`, `create_dns_provider()`
- Leading underscore for private/internal: `_parse_bool()`, `_is_domain_excluded()`
- No special prefix for async (none used in codebase)

**Variables:**
- UPPERCASE_WITH_UNDERSCORES for module-level config: `DNS_PROVIDER`, `ADGUARD_URL`
- snake_case for local variables: `dns_provider`, `proxy_routes`
- Leading underscore for private instance vars: `self._url`, `self._auth`

**Types:**
- PascalCase for classes: `DNSRecord`, `ProxyRoute`, `ExternalDNSSyncer`
- PascalCase for enums: `DNSZone`
- No `I` prefix for interfaces (use ABC directly)

## Code Style

**Formatting:**
- Ruff formatter with config in `pyproject.toml`
- Line length: 100 characters
- Quote style: Double quotes
- Semicolons: Not used (Python)
- Indentation: 4 spaces for Python, 2 for YAML

**Linting:**
- Ruff with rules: E, F, I, B (E501 ignored)
- Config: `pyproject.toml` [tool.ruff] section
- Run: `make lint` or `ruff check .`

## Import Organization

**Order:**
1. Future imports: `from __future__ import annotations`
2. Standard library: `import json`, `import logging`, `from pathlib import Path`
3. Third-party packages: `import requests`, `import yaml`
4. Type checking imports: `from typing import TYPE_CHECKING`

**Grouping:**
- Blank line between groups
- Alphabetical within groups
- `from` imports after `import` statements

**Path Aliases:**
- None used (standard relative imports)

## Error Handling

**Patterns:**
- Try/except at API boundaries (HTTP calls)
- Return `False` for operation failures (add, delete, update)
- Return empty list for retrieval failures (get_records, get_routes)
- Log errors with context before returning

**Error Types:**
- `requests.exceptions.RequestException` for HTTP errors
- `ValueError` for validation failures
- Generic `Exception` catch at top level with logging

**Logging:**
- Log at ERROR level for failures
- Log at WARNING level for recoverable issues
- Log at INFO level for successful operations
- Log at DEBUG level for detailed tracing

## Logging

**Framework:**
- Python stdlib `logging` module
- Logger instance: `logger = logging.getLogger(__name__)`
- Levels: DEBUG, INFO, WARNING, ERROR

**Patterns:**
- Structured messages: `logger.info("Added DNS record %s -> %s", domain, answer)`
- Context in format string: `logger.error("Failed to get routes from %s: %s", url, err)`
- No f-strings in log calls (use % formatting for lazy evaluation)

## Comments

**When to Comment:**
- Explain business logic: zone detection rules, filtering patterns
- Document environment variables and their purpose
- Explain non-obvious algorithm choices
- Mark sections with separator comments

**Section Comments:**
```python
# =============================================================================
# File Watching Utilities
# =============================================================================
```

**Docstrings:**
- Module-level: Comprehensive usage documentation (lines 1-95)
- Class-level: Brief purpose description
- Function-level: One-liner for simple, multi-line with Args/Returns for complex

**TODO Comments:**
- Format: `# TODO: description`
- No username tracking (use git blame)
- Not heavily used in this codebase

## Function Design

**Size:**
- Most functions under 50 lines
- Some larger methods exist: `sync_once()` (188 lines), `get_instances()` (100 lines)
- Extract helpers for repeated logic

**Parameters:**
- Max 3-4 parameters for most functions
- Use dataclasses for complex parameter groups: `ProxyInstance`
- Type hints on all parameters and return values

**Return Values:**
- Explicit return types via type hints
- `bool` for success/failure operations
- `list[T]` for retrieval operations (empty on error)
- `None` for void operations

## Module Design

**Exports:**
- Single module design (`cli.py` contains everything)
- Console script entry point in `pyproject.toml`
- `__init__.py` exports version string only

**Organization:**
- Sections marked with separator comments
- Order: imports, config, logging, enums, dataclasses, ABCs, implementations, utilities, main

**Monolithic Pattern:**
- All code in single `cli.py` file (1,235 lines)
- Rationale: Simple deployment, no import complexity
- Future consideration: Split into modules if it grows significantly

---

*Convention analysis: 2025-12-28*
*Update when patterns change*
