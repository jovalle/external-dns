# Testing Patterns

**Analysis Date:** 2025-12-28

## Test Framework

**Runner:**
- pytest 8.3+
- Config: `pyproject.toml` [tool.pytest.ini_options] section

**Assertion Library:**
- pytest built-in `assert` statements
- No additional assertion libraries

**Run Commands:**
```bash
make test                              # Run unit tests
make test-integration                  # Run Docker integration tests
pytest                                 # Direct pytest invocation
pytest tests/test_utils.py            # Single file
pytest -v                             # Verbose output
```

## Test File Organization

**Location:**
- Unit tests: `tests/` directory
- Integration tests: `tests/integration/` directory

**Naming:**
- Unit tests: `test_{module}.py` (e.g., `test_utils.py`, `test_config_watch.py`)
- Integration tests: `test_{feature}.py` (e.g., `test_docker_stack.py`)

**Structure:**
```
tests/
├── test_utils.py              # Utility function tests
├── test_config_watch.py       # Config file watching tests
└── integration/
    └── test_docker_stack.py   # Docker-based E2E tests
```

## Test Structure

**Suite Organization:**
```python
from external_dns.cli import _parse_static_rewrites, _is_domain_excluded

def test_parse_static_rewrites_empty() -> None:
    """Test empty input returns empty dict."""
    assert _parse_static_rewrites("", "1.2.3.4") == {}

def test_parse_static_rewrites_domain_only_uses_default_ip() -> None:
    """Test domain without IP uses default."""
    result = _parse_static_rewrites("example.com", "1.2.3.4")
    assert result == {"example.com": "1.2.3.4"}
```

**Patterns:**
- No `describe` blocks (flat test functions)
- Type hints on all test functions: `-> None`
- Descriptive function names: `test_{function}_{scenario}`
- Single assertion focus per test
- pytest fixtures for shared setup

## Mocking

**Framework:**
- pytest fixtures (built-in `tmp_path`)
- No extensive mocking library used

**Patterns:**
```python
def test_find_config_files_directory_excludes_template(tmp_path: Path) -> None:
    """Test .template files are excluded."""
    (tmp_path / "a.yaml").write_text("test")
    (tmp_path / "b.yaml.template").write_text("test")

    result = find_config_files(str(tmp_path))

    assert len(result) == 1
    assert "a.yaml" in result[0]
```

**What to Mock:**
- File system (via `tmp_path` fixture)
- HTTP calls (not currently mocked, tested via integration tests)

**What NOT to Mock:**
- Pure utility functions (test directly)
- Simple parsing logic

## Fixtures and Factories

**Test Data:**
```python
# Inline test data for simple cases
def test_parse_exclude_patterns_exact_wildcard_and_regex() -> None:
    patterns = _parse_exclude_patterns("foo.com,*.bar.com,~^staging-\\d+\\.example\\.com$")

    assert _is_domain_excluded("foo.com", patterns)
    assert _is_domain_excluded("sub.bar.com", patterns)
    assert _is_domain_excluded("staging-123.example.com", patterns)
```

**Location:**
- Inline in test files (no separate fixtures directory)
- `tmp_path` fixture for filesystem tests

## Coverage

**Requirements:**
- No enforced coverage threshold
- Coverage tracked for awareness

**Configuration:**
- pytest-cov 5.0+ available
- Not configured for automatic reporting

**View Coverage:**
```bash
pytest --cov=src/external_dns --cov-report=html
open htmlcov/index.html
```

## Test Types

**Unit Tests:**
- Scope: Individual utility functions
- Location: `tests/test_utils.py`, `tests/test_config_watch.py`
- Mocking: Minimal (filesystem via tmp_path)
- Speed: Fast (<1s per test)

**Integration Tests:**
- Scope: Full Docker stack with AdGuard, Traefik, external-dns
- Location: `tests/integration/test_docker_stack.py`
- Marker: `@pytest.mark.integration`
- Requires: `EXTERNAL_DNS_RUN_DOCKER_TESTS=1` env var
- Speed: Slow (starts Docker containers)

**E2E Tests:**
- Covered by integration tests (Docker-based end-to-end)

## Common Patterns

**Async Testing:**
- Not applicable (no async code in codebase)

**Error Testing:**
```python
def test_parse_static_rewrites_invalid_format() -> None:
    """Test graceful handling of invalid input."""
    result = _parse_static_rewrites("invalid===format", "1.2.3.4")
    assert result == {}  # Returns empty, doesn't throw
```

**Integration Test Helper Pattern:**
```python
def _run(cmd: str) -> str:
    """Execute shell command and return output."""
    return subprocess.check_output(cmd, shell=True, text=True)

def _step(name: str) -> None:
    """Log test step for debugging."""
    print(f"\n=== {name} ===")

def _wait_for_container_health(container: str, timeout: int = 60) -> bool:
    """Wait for Docker container to be healthy."""
    # Implementation...
```

**Conditional Skipping:**
```python
@pytest.mark.integration
def test_local_stack_syncs_traefik_routes_to_adguard() -> None:
    if not os.environ.get("EXTERNAL_DNS_RUN_DOCKER_TESTS"):
        pytest.skip("Set EXTERNAL_DNS_RUN_DOCKER_TESTS=1 to run")
    if not _docker_available():
        pytest.skip("docker not available")
    # Test implementation...
```

**Snapshot Testing:**
- Not used in this codebase

---

*Testing analysis: 2025-12-28*
*Update when test patterns change*
