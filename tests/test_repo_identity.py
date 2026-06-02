from pathlib import Path

from portmap.repo_identity import resolve_repo_identity


def test_repo_identity_uses_portmap_repo_override(tmp_path: Path) -> None:
    config_dir = tmp_path / ".portmap"
    config_dir.mkdir()
    (config_dir / "repo.toml").write_text(
        'repo_id = "custom-repo"\ndisplay_name = "Custom Repo"\n',
        encoding="utf-8",
    )

    identity = resolve_repo_identity(
        project_directory=tmp_path,
        repo_root=None,
        repo_id=None,
        repo_name=None,
    )

    assert identity.repo_id == "custom-repo"
    assert identity.display_name == "Custom Repo"

