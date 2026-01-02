from pathlib import Path

from external_dns.cli import find_config_files


def test_find_config_files_directory_excludes_template(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("instances: []\n", encoding="utf-8")
    (tmp_path / "b.yaml.template").write_text("instances: []\n", encoding="utf-8")
    (tmp_path / "c.yaml").write_text("instances: []\n", encoding="utf-8")

    files = find_config_files(str(tmp_path))
    assert [Path(f).name for f in files] == ["a.yaml", "c.yaml"]
