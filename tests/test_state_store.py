"""Unit tests for StateStore."""

import json
from pathlib import Path

from external_dns.cli import StateStore


class TestStateStoreLoad:
    """Tests for StateStore load functionality."""

    def test_load_returns_default_state_when_file_missing(self, tmp_path: Path) -> None:
        """Test load returns default state when file doesn't exist."""
        state_file = tmp_path / "nonexistent" / "state.json"
        store = StateStore(str(state_file))

        state = store.load()

        assert state == {"version": 1, "instances": {}, "domains": {}}

    def test_load_returns_file_contents(self, tmp_path: Path) -> None:
        """Test load returns parsed content from valid JSON file."""
        state_file = tmp_path / "state.json"
        expected_state = {
            "version": 1,
            "instances": {"traefik": {"last_success": 1234567890}},
            "domains": {"app.example.com": {"sources": {"traefik": {"answer": "10.0.0.1"}}}},
        }
        state_file.write_text(json.dumps(expected_state))

        store = StateStore(str(state_file))
        state = store.load()

        assert state == expected_state

    def test_load_returns_default_on_invalid_json(self, tmp_path: Path) -> None:
        """Test load returns default state on corrupted/invalid JSON file."""
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json {{{")

        store = StateStore(str(state_file))
        state = store.load()

        assert state == {"version": 1, "instances": {}, "domains": {}}


class TestStateStoreSave:
    """Tests for StateStore save functionality."""

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        """Test save creates parent directories if they don't exist."""
        state_file = tmp_path / "nested" / "path" / "state.json"
        store = StateStore(str(state_file))
        state = {"version": 1, "instances": {}, "domains": {}}

        store.save(state)

        assert state_file.exists()
        assert state_file.parent.exists()

    def test_save_writes_valid_json(self, tmp_path: Path) -> None:
        """Test save writes valid JSON that can be parsed."""
        state_file = tmp_path / "state.json"
        store = StateStore(str(state_file))
        state = {
            "version": 1,
            "instances": {"traefik": {"last_success": 1234567890}},
            "domains": {"app.example.com": {"sources": {"traefik": {"answer": "10.0.0.1"}}}},
        }

        store.save(state)

        # Read and parse the file content
        content = state_file.read_text()
        parsed = json.loads(content)
        assert parsed == state

    def test_save_atomic_via_temp_file(self, tmp_path: Path) -> None:
        """Test save uses temp file + rename for atomic writes."""
        state_file = tmp_path / "state.json"
        store = StateStore(str(state_file))
        state = {"version": 1, "instances": {}, "domains": {}}

        # Save the state
        store.save(state)

        # Verify the final file exists and temp file does not
        assert state_file.exists()
        temp_file = state_file.with_suffix(".json.tmp")
        assert not temp_file.exists()

        # Verify content is correct
        content = json.loads(state_file.read_text())
        assert content == state

    def test_save_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Test save overwrites existing file content."""
        state_file = tmp_path / "state.json"
        initial_state = {"version": 1, "instances": {}, "domains": {"old.example.com": {}}}
        state_file.write_text(json.dumps(initial_state))

        store = StateStore(str(state_file))
        new_state = {"version": 1, "instances": {}, "domains": {"new.example.com": {}}}
        store.save(new_state)

        content = json.loads(state_file.read_text())
        assert content == new_state
        assert "old.example.com" not in content["domains"]


class TestStateStoreStructure:
    """Tests for StateStore state structure."""

    def test_default_state_has_required_keys(self, tmp_path: Path) -> None:
        """Test default state has version, instances, and domains keys."""
        state_file = tmp_path / "nonexistent.json"
        store = StateStore(str(state_file))

        state = store.load()

        assert "version" in state
        assert "instances" in state
        assert "domains" in state
        assert state["version"] == 1
        assert isinstance(state["instances"], dict)
        assert isinstance(state["domains"], dict)


class TestStateStorePath:
    """Tests for StateStore path handling."""

    def test_store_path_is_pathlib_path(self, tmp_path: Path) -> None:
        """Test store path is converted to pathlib Path."""
        state_file = tmp_path / "state.json"
        store = StateStore(str(state_file))

        assert isinstance(store.path, Path)
        assert store.path == state_file

    def test_save_formats_json_with_indentation(self, tmp_path: Path) -> None:
        """Test saved JSON is formatted with indentation for readability."""
        state_file = tmp_path / "state.json"
        store = StateStore(str(state_file))
        state = {"version": 1, "instances": {}, "domains": {}}

        store.save(state)

        content = state_file.read_text()
        # Check that JSON is formatted with indentation (not single line)
        assert "\n" in content
        assert "  " in content  # Indent characters present

    def test_save_sorts_keys_for_deterministic_output(self, tmp_path: Path) -> None:
        """Test saved JSON has sorted keys for deterministic output."""
        state_file = tmp_path / "state.json"
        store = StateStore(str(state_file))
        # Create state with keys that would be in different order unsorted
        state = {
            "domains": {"z.example.com": {}, "a.example.com": {}},
            "version": 1,
            "instances": {},
        }

        store.save(state)

        content = state_file.read_text()
        # Verify keys are sorted alphabetically in output
        domains_pos = content.find('"domains"')
        instances_pos = content.find('"instances"')
        version_pos = content.find('"version"')
        assert domains_pos < instances_pos < version_pos
