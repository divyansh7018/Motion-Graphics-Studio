"""Assets: import, missing files, relinking (directive sections 21, 22, 23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.project.assets import (
    AssetImportError,
    detect_kind,
    file_checksum,
    ignore_missing_asset,
    relink_asset,
    replace_asset,
    update_missing_flags,
    verify_assets,
)
from app.project.model import ElementSpec
from app.project.service import CreateRequest, ProjectService

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def service(paths, settings):
    return ProjectService(paths, settings)


@pytest.fixture()
def open_project(service):
    service.create_project(CreateRequest(name="Assets Project"))
    return service


def write_png(path: Path, size: int = 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_HEADER + bytes(size))
    return path


def test_asset_kinds_are_detected_from_the_file_name() -> None:
    assert detect_kind(Path("logo.png")) == "image"
    assert detect_kind(Path("brand.svg")) == "svg"
    assert detect_kind(Path("clip.MP4")) == "video"
    assert detect_kind(Path("voice.wav")) == "audio"
    assert detect_kind(Path("font.ttf")) == "font"
    assert detect_kind(Path("notes.pdf")) == "other"


def test_importing_copies_the_file_into_the_project(open_project, tmp_path) -> None:
    service = open_project
    source = write_png(tmp_path / "incoming" / "logo.png")

    report = service.import_asset(source, kind="logo")

    assert report.ok and report.copied
    asset = report.asset
    assert asset.path == "assets/logo.png"
    assert asset.absolute_path == ""
    assert asset.is_portable is True
    assert (service.current_layout.assets_dir / "logo.png").exists()
    assert asset.size_bytes == source.stat().st_size
    assert asset.checksum


def test_a_second_import_does_not_overwrite_the_first(open_project, tmp_path) -> None:
    service = open_project
    first = write_png(tmp_path / "a" / "logo.png")
    second = write_png(tmp_path / "b" / "logo.png", size=128)

    service.import_asset(first)
    report = service.import_asset(second)

    assert report.asset.path == "assets/logo2.png"
    assert (service.current_layout.assets_dir / "logo.png").exists()
    assert (service.current_layout.assets_dir / "logo2.png").exists()


def test_referencing_in_place_is_marked_as_not_portable(open_project, tmp_path) -> None:
    service = open_project
    source = write_png(tmp_path / "elsewhere" / "bg.png")

    report = service.import_asset(source, copy=False)

    assert report.asset.path == ""
    assert report.asset.absolute_path.endswith("bg.png")
    assert report.asset.is_portable is False
    assert any("not portable" in note.lower() or "another PC" in note for note in report.notes)
    assert not (service.current_layout.assets_dir / "bg.png").exists()


def test_missing_and_empty_files_are_refused_with_a_reason(open_project, tmp_path) -> None:
    service = open_project

    assert service.import_asset(tmp_path / "nope.png").error
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert service.import_asset(empty).error
    assert service.import_asset(tmp_path).error, "a folder is not a file"
    assert service.current.assets == []


def test_a_missing_asset_is_reported_without_crashing(open_project, tmp_path) -> None:
    service = open_project
    source = write_png(tmp_path / "src" / "gone.png")
    service.import_asset(source)
    (service.current_layout.assets_dir / "gone.png").unlink()

    checks = verify_assets(service.current, service.current_layout)

    assert checks[0].exists is False
    assert "MISSING" in checks[0].to_line()
    missing = update_missing_flags(service.current, service.current_layout)
    assert [asset.name for asset in missing] == ["gone.png"]
    assert service.current.assets[0].missing is True


def test_relinking_keeps_the_id_and_the_scene_reference(open_project, tmp_path) -> None:
    service = open_project
    source = write_png(tmp_path / "src" / "bg.png")
    asset = service.import_asset(source).asset
    scene = service.add_scene("image", "Background")
    scene.elements.append(ElementSpec(id="el-1", kind="image", asset_id=asset.id))
    (service.current_layout.assets_dir / "bg.png").unlink()

    replacement = write_png(tmp_path / "new" / "background.png", size=200)
    assert service.relink_asset(asset.id, replacement) is True

    assert asset.missing is False
    assert asset.path.startswith("assets/")
    assert service.current.asset_references(asset.id) == ["scene 'Background'"]
    assert verify_assets(service.current, service.current_layout)[0].exists is True


def test_relinking_to_a_missing_file_is_refused(open_project, tmp_path) -> None:
    service = open_project
    asset = service.import_asset(write_png(tmp_path / "src" / "x.png")).asset

    with pytest.raises(AssetImportError):
        relink_asset(service.current, service.current_layout, asset, tmp_path / "not-there.png")


def test_replacing_keeps_the_asset_name(open_project, tmp_path) -> None:
    service = open_project
    asset = service.import_asset(write_png(tmp_path / "a" / "hero.png")).asset

    replace_asset(service.current, service.current_layout, asset, write_png(tmp_path / "b" / "other.png", size=99))

    assert asset.name == "hero.png"
    assert asset.size_bytes == len(PNG_HEADER) + 99


def test_ignoring_a_missing_asset_keeps_the_project_usable(open_project, tmp_path) -> None:
    service = open_project
    asset = service.import_asset(write_png(tmp_path / "a" / "maybe.png")).asset
    (service.current_layout.assets_dir / "maybe.png").unlink()

    ignore_missing_asset(service.current, asset)

    assert asset.missing is True
    assert "ignored" in asset.notes.lower()
    assert service.validate().ok, "an ignored asset must not block saving"


def test_deleting_an_asset_can_remove_its_file_inside_the_project_only(open_project, tmp_path) -> None:
    from app.project.assets import delete_asset

    service = open_project
    copied = service.import_asset(write_png(tmp_path / "a" / "copy.png")).asset
    referenced = service.import_asset(write_png(tmp_path / "b" / "ref.png"), copy=False).asset

    assert delete_asset(service.current, service.current_layout, copied.id, delete_file=True) is True
    assert not (service.current_layout.assets_dir / "copy.png").exists()

    assert delete_asset(service.current, service.current_layout, referenced.id, delete_file=True) is True
    assert Path(referenced.absolute_path).exists(), "a file outside the project is never deleted"


def test_checksums_are_stable_and_bounded(tmp_path) -> None:
    file_a = tmp_path / "a.bin"
    file_a.write_bytes(b"hello world")
    file_b = tmp_path / "b.bin"
    file_b.write_bytes(b"hello world")

    assert file_checksum(file_a) == file_checksum(file_b)
    assert file_checksum(tmp_path / "missing.bin") == ""


def test_a_project_with_a_missing_asset_still_opens(service, tmp_path) -> None:
    """Directive 36-7: a missing asset must never stop a project opening."""
    service.create_project(CreateRequest(name="Missing Asset"))
    layout = service.current_layout
    service.import_asset(write_png(tmp_path / "a" / "logo.png"))
    service.save()
    (layout.assets_dir / "logo.png").unlink()
    service.close_project()

    reopened = service.open_project(layout.root)

    assert reopened.project.name == "Missing Asset"
    assert any("asset" in note.lower() for note in service.session.notes)
    assert reopened.assets[0].missing is True
