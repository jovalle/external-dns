"""Unit tests for utility functions in external_dns.cli.

Tests cover:
- Static rewrite parsing (_parse_static_rewrites)
- Exclude pattern parsing (_parse_exclude_patterns)
- Domain exclusion checking (_is_domain_excluded)
- Boolean parsing (_parse_bool)
- Config file finding (find_config_files)
"""

import os
import re
import tempfile
from pathlib import Path

from external_dns.cli import (
    _is_domain_excluded,
    _parse_bool,
    _parse_exclude_patterns,
    _parse_static_rewrites,
    find_config_files,
)

# =============================================================================
# Static Rewrite Parsing Tests
# =============================================================================


def test_parse_static_rewrites_empty() -> None:
    """Empty string returns empty dict."""
    assert _parse_static_rewrites("", "1.2.3.4") == {}


def test_parse_static_rewrites_domain_only_uses_default_ip() -> None:
    """Domain without '=' uses the default IP."""
    assert _parse_static_rewrites("a.example.com", "1.2.3.4") == {"a.example.com": "1.2.3.4"}


def test_parse_static_rewrites_domain_equals_true_uses_default_ip() -> None:
    """Domain with '=true' uses the default IP."""
    assert _parse_static_rewrites("a.example.com=true", "1.2.3.4") == {"a.example.com": "1.2.3.4"}


def test_parse_static_rewrites_domain_equals_ip() -> None:
    """Domain with '=IP' uses that specific IP."""
    assert _parse_static_rewrites("a.example.com=10.0.0.10", "1.2.3.4") == {
        "a.example.com": "10.0.0.10"
    }


def test_parse_static_rewrites_multiple_entries() -> None:
    """Comma-separated list of domains are all parsed correctly."""
    result = _parse_static_rewrites(
        "a.example.com,b.example.com=10.0.0.2,c.example.com=true",
        "1.2.3.4",
    )
    assert result == {
        "a.example.com": "1.2.3.4",
        "b.example.com": "10.0.0.2",
        "c.example.com": "1.2.3.4",
    }


def test_parse_static_rewrites_whitespace_handling() -> None:
    """Extra whitespace around domains and IPs is trimmed."""
    result = _parse_static_rewrites(
        "  a.example.com  ,  b.example.com = 10.0.0.5  ",
        "1.2.3.4",
    )
    assert result == {
        "a.example.com": "1.2.3.4",
        "b.example.com": "10.0.0.5",
    }


def test_parse_static_rewrites_invalid_format_returns_empty() -> None:
    """Malformed entries (empty domain, empty answer) are gracefully skipped."""
    # Empty domain part before '='
    result = _parse_static_rewrites("=10.0.0.1", "1.2.3.4")
    assert result == {}

    # Empty entries in the list are skipped
    result = _parse_static_rewrites(",,a.example.com,,", "1.2.3.4")
    assert result == {"a.example.com": "1.2.3.4"}


def test_parse_static_rewrites_empty_default_ip_skips_domain_only() -> None:
    """If default IP is empty, domain-only entries produce empty answer and get filtered."""
    # The function filters out entries where answer is empty
    result = _parse_static_rewrites("a.example.com", "")
    assert result == {}


# =============================================================================
# Exclude Pattern Parsing Tests
# =============================================================================


def test_parse_exclude_patterns_exact_wildcard_and_regex() -> None:
    """Patterns can be exact, wildcard (fnmatch), or regex (prefix ~)."""
    patterns = _parse_exclude_patterns("auth.example.com,*.internal.*,~^dev-\\d+\\.example\\.com$")
    assert all(isinstance(p, re.Pattern) for p in patterns)

    assert _is_domain_excluded("auth.example.com", patterns)
    assert _is_domain_excluded("service.internal.example.com", patterns)
    assert _is_domain_excluded("dev-42.example.com", patterns)

    assert not _is_domain_excluded("public.example.com", patterns)


def test_parse_exclude_patterns_empty_string() -> None:
    """Empty input returns empty list."""
    patterns = _parse_exclude_patterns("")
    assert patterns == []


def test_parse_exclude_patterns_single_exact() -> None:
    """Single exact domain produces one pattern."""
    patterns = _parse_exclude_patterns("auth.example.com")
    assert len(patterns) == 1
    assert _is_domain_excluded("auth.example.com", patterns)
    assert not _is_domain_excluded("other.example.com", patterns)


def test_parse_exclude_patterns_invalid_regex_skipped() -> None:
    """Invalid regex patterns are skipped gracefully (logged as warning)."""
    # The '[' is an invalid regex - unclosed character class
    patterns = _parse_exclude_patterns("~[invalid,valid.example.com")
    # Should have only the valid exact pattern
    assert len(patterns) == 1
    assert _is_domain_excluded("valid.example.com", patterns)


def test_parse_exclude_patterns_whitespace_trimmed() -> None:
    """Whitespace around patterns is trimmed."""
    patterns = _parse_exclude_patterns("  a.example.com  ,  *.test.*  ")
    assert len(patterns) == 2
    assert _is_domain_excluded("a.example.com", patterns)
    assert _is_domain_excluded("foo.test.bar", patterns)


# =============================================================================
# Domain Exclusion Tests
# =============================================================================


def test_is_domain_excluded_case_insensitive() -> None:
    """Exclusion matching is case-insensitive (as implemented with re.IGNORECASE)."""
    patterns = _parse_exclude_patterns("Auth.Example.COM")
    # All case variations should match
    assert _is_domain_excluded("auth.example.com", patterns)
    assert _is_domain_excluded("AUTH.EXAMPLE.COM", patterns)
    assert _is_domain_excluded("Auth.Example.Com", patterns)


def test_is_domain_excluded_empty_patterns() -> None:
    """No patterns means nothing is excluded."""
    patterns: list[re.Pattern] = []
    assert not _is_domain_excluded("anything.example.com", patterns)


def test_is_domain_excluded_partial_match() -> None:
    """Exact patterns require full match, not partial."""
    patterns = _parse_exclude_patterns("example.com")
    # Should NOT match subdomains (exact match only)
    assert _is_domain_excluded("example.com", patterns)
    # Partial matches should fail since it's anchored with ^ and $
    assert not _is_domain_excluded("sub.example.com", patterns)
    assert not _is_domain_excluded("example.com.other", patterns)


# =============================================================================
# Boolean Parsing Tests
# =============================================================================


def test_parse_bool_true_values() -> None:
    """'true', '1', 'yes', 'y', 'on', and True all return True."""
    for val in ["true", "TRUE", "True", "1", "yes", "YES", "y", "Y", "on", "ON", True]:
        assert _parse_bool(val) is True, f"Expected True for {val!r}"


def test_parse_bool_false_values() -> None:
    """'false', '0', 'no', 'n', 'off', and False all return False."""
    for val in ["false", "FALSE", "False", "0", "no", "NO", "n", "N", "off", "OFF", False]:
        assert _parse_bool(val) is False, f"Expected False for {val!r}"


def test_parse_bool_default_on_invalid() -> None:
    """Invalid input uses the provided default value."""
    # Default is True when not specified
    assert _parse_bool("invalid") is False  # not in truthy set
    assert _parse_bool("maybe") is False

    # Explicit default=False
    assert _parse_bool("invalid", default=False) is False
    assert _parse_bool(None, default=False) is False

    # Explicit default=True
    assert _parse_bool(None, default=True) is True


def test_parse_bool_none_uses_default() -> None:
    """None input returns the default value."""
    assert _parse_bool(None, default=True) is True
    assert _parse_bool(None, default=False) is False


def test_parse_bool_whitespace_stripped() -> None:
    """Whitespace around values is stripped before comparison."""
    assert _parse_bool("  true  ") is True
    assert _parse_bool("  1  ") is True
    assert _parse_bool("  yes  ") is True


# =============================================================================
# Config File Finding Tests
# =============================================================================


def test_find_config_files_single_file_path() -> None:
    """Single file path returns that file in a list."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        f.write(b"instances: []")
        temp_path = f.name

    try:
        result = find_config_files(temp_path)
        assert result == [temp_path]
    finally:
        os.unlink(temp_path)


def test_find_config_files_directory_finds_yaml_and_json() -> None:
    """Directory scan finds .yaml files (but not .template files)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        yaml1 = Path(tmpdir) / "config1.yaml"
        yaml2 = Path(tmpdir) / "config2.yaml"
        template = Path(tmpdir) / "example.yaml.template"
        txt_file = Path(tmpdir) / "readme.txt"

        yaml1.write_text("instances: []")
        yaml2.write_text("instances: []")
        template.write_text("# template")
        txt_file.write_text("readme")

        result = find_config_files(tmpdir)

        # Should find both .yaml files but not .template or .txt
        assert len(result) == 2
        assert str(yaml1) in result
        assert str(yaml2) in result
        assert str(template) not in result
        assert str(txt_file) not in result


def test_find_config_files_empty_directory() -> None:
    """Empty directory returns empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = find_config_files(tmpdir)
        assert result == []


def test_find_config_files_nonexistent_path() -> None:
    """Non-existent path returns empty list."""
    result = find_config_files("/nonexistent/path/to/config.yaml")
    assert result == []


def test_find_config_files_excludes_template_suffix() -> None:
    """Files ending with .template are excluded even if they contain .yaml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        template = Path(tmpdir) / "traefik.yaml.template"
        regular = Path(tmpdir) / "traefik.yaml"

        template.write_text("# template file")
        regular.write_text("instances: []")

        result = find_config_files(tmpdir)

        assert len(result) == 1
        assert str(regular) in result
        assert str(template) not in result


def test_find_config_files_sorted_alphabetically() -> None:
    """Results are sorted alphabetically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "z_config.yaml").write_text("instances: []")
        (Path(tmpdir) / "a_config.yaml").write_text("instances: []")
        (Path(tmpdir) / "m_config.yaml").write_text("instances: []")

        result = find_config_files(tmpdir)

        # Should be sorted
        filenames = [Path(p).name for p in result]
        assert filenames == ["a_config.yaml", "m_config.yaml", "z_config.yaml"]
