"""Path resolution and filename safety (directive sections 10, 11, 36, 69)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.paths import (
    DATA_ROOT_ENV_VAR,
    AppPaths,
    PathResolutionError,
    default_user_data_root,
    directory_is_writable,
    is_within,
    read_data_root_pointer,
    resolve_data_root,
    safe_filename,
    unique_path,
    write_data_root_pointer,
)


# --------------------------------------------------------------------------
# is_within - the guard used by every cleanup path
# --------------------------------------------------------------------------

def test_is_within_accepts_children(tmp_path: Path) -> None:
    parent = tmp_path / "root"
    (parent / "a" / "b").mkdir(parents=True)
    assert is_within(parent / "a" / "b", parent)
    assert is_within(parent, parent)


def test_is_within_rejects_outside_and_traversal(tmp_path: Path) -> None:
    parent = tmp_path / "root"
    other = tmp_path / "other"
    parent.mkdir()
    other.mkdir()
    assert not is_within(other, parent)
    # ".." must not be able to escape the parent.
    assert not is_within(parent / ".." / "other", parent)


# --------------------------------------------------------------------------
# Data root resolution order
# --------------------------------------------------------------------------

def test_command_line_override_wins(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    override = tmp_path / "override"
    monkeypatch.setenv(DATA_ROOT_ENV_VAR, str(tmp_path / "from-env"))

    chosen, reason = resolve_data_root(source, cli_override=override, env=os.environ)

    assert chosen == override.resolve()
    assert "command-line" in reason


def test_environment_variable_used_when_no_override(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    env_root = tmp_path / "from-env"
    monkeypatch.setenv(DATA_ROOT_ENV_VAR, str(env_root))

    chosen, reason = resolve_data_root(source, env=os.environ)

    assert chosen == env_root.resolve()
    assert DATA_ROOT_ENV_VAR in reason


def test_remembered_root_used_before_portable(tmp_path: Path, home: Path = None) -> None:
    source = tmp_path / "source"
    source.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    remembered = tmp_path / "remembered"
    remembered.mkdir()

    env = {"HOME": str(fake_home)}
    write_data_root_pointer(remembered, env=env, home=fake_home, platform_name="posix")

    chosen, reason = resolve_data_root(source, env=env, home=fake_home, platform_name="posix")

    assert chosen == remembered.resolve()
    assert "remembered" in reason


def test_portable_install_used_when_writable(tmp_path: Path) -> None:
    source = tmp_path / "portable"
    source.mkdir()
    env = {"HOME": str(tmp_path / "home")}

    chosen, reason = resolve_data_root(source, env=env, home=tmp_path / "home", platform_name="posix")

    assert chosen == source.resolve()
    assert "portable" in reason


def test_fallback_is_per_user_location(tmp_path: Path, monkeypatch) -> None:
    """When the application folder cannot be written to, data goes to the user profile."""
    from app.core import paths as paths_module

    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "program-files-install"
    source.mkdir()
    env = {"HOME": str(home)}

    monkeypatch.setattr(paths_module, "directory_is_writable", lambda _directory: False)
    expected = default_user_data_root(env=env, home=home, platform_name="posix")
    chosen, reason = resolve_data_root(source, env=env, home=home, platform_name="posix")

    assert chosen == expected
    assert "per-user" in reason


def test_missing_source_folder_is_not_used_for_portable_data(tmp_path: Path) -> None:
    """A source folder that does not exist must never be created as a data root."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home)}
    missing_source = tmp_path / "never-existed"

    chosen, _reason = resolve_data_root(missing_source, env=env, home=home, platform_name="posix")

    assert chosen != missing_source
    assert not missing_source.exists()


def test_windows_data_root_uses_localappdata(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
    resolved = default_user_data_root(env=env, home=tmp_path, platform_name="nt")
    assert resolved == tmp_path / "AppData" / "Local" / "MotionGraphicsStudio"


def test_pointer_file_ignores_relative_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home)}
    pointer = home / ".local" / "share" / "motion-graphics-studio" / "data_root.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("relative/path", encoding="utf-8")

    assert read_data_root_pointer(env=env, home=home, platform_name="posix") is None


# --------------------------------------------------------------------------
# AppPaths
# --------------------------------------------------------------------------

def test_app_paths_creates_every_folder(data_root: Path) -> None:
    from app.core.paths import DATA_SUBDIRECTORIES

    instance = AppPaths(data_root=data_root, source_root=data_root.parent)
    created = instance.ensure()

    assert len(created) == len(DATA_SUBDIRECTORIES)
    for name in DATA_SUBDIRECTORIES:
        assert (data_root / name).is_dir()


def test_app_paths_reports_unwritable_root(tmp_path: Path, monkeypatch) -> None:
    """A read-only data root must produce a friendly error, not a traceback."""

    blocked = tmp_path / "blocked"
    blocked.mkdir()

    real_mkdir = Path.mkdir

    def exploding_mkdir(self, *args, **kwargs):  # noqa: ANN001 - test double
        if str(self).startswith(str(blocked)):
            raise PermissionError(13, "Access is denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", exploding_mkdir)
    instance = AppPaths(data_root=blocked, source_root=tmp_path)
    with pytest.raises(PathResolutionError) as excinfo:
        instance.ensure()

    message = str(excinfo.value)
    assert "could not create" in message.lower()
    assert str(blocked) in message


def test_project_dir_rejects_path_traversal(data_root: Path) -> None:
    instance = AppPaths(data_root=data_root, source_root=data_root)
    instance.ensure()

    folder = instance.project_dir("../../etc/passwd")
    assert instance.is_inside_data_root(folder)

    with pytest.raises(ValueError):
        instance.project_dir("///")


def test_directory_is_writable_is_honest(tmp_path: Path) -> None:
    assert directory_is_writable(tmp_path)
    missing_parent = tmp_path / "a" / "b" / "c"
    assert directory_is_writable(missing_parent)  # created on demand
    assert not list(missing_parent.glob(".mgs_write_test_*"))  # probe cleaned up


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------

def test_safe_filename_strips_invalid_characters() -> None:
    assert safe_filename('My<Project>:"/\\|?*') == "My_Project"
    assert safe_filename("   ") == "untitled"
    assert safe_filename("CON") == "CON_file"
    assert safe_filename("LPT1") == "LPT1_file"
    assert len(safe_filename("x" * 500)) <= 120


def test_unique_path_numbers_new_files(tmp_path: Path) -> None:
    first = unique_path(tmp_path, "Video", ".mp4")
    assert first.name == "Video.mp4"
    first.write_bytes(b"x")

    second = unique_path(tmp_path, "Video", ".mp4")
    assert second.name == "Video2.mp4"
    second.write_bytes(b"x")

    third = unique_path(tmp_path, "Video", ".mp4")
    assert third.name == "Video3.mp4"
    # The originals must still exist - outputs are never overwritten.
    assert first.exists() and second.exists()


def test_unique_path_accepts_suffix_without_dot(tmp_path: Path) -> None:
    assert unique_path(tmp_path, "Clip", "mp4").suffix == ".mp4"
