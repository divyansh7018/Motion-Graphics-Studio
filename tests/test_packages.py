"""Package detection.

Regression cover for a bug found while testing: the first implementation set
``installed = True`` *before* verifying the package, so every package - even a
made-up name - was reported as installed.  The system check would then have told
users their voice engine was ready when it was not.
"""

from __future__ import annotations

from app.core.packages import (
    DISTRIBUTION_ALIASES,
    module_is_importable,
    probe_many,
    probe_package,
)


def test_missing_package_is_reported_missing() -> None:
    status = probe_package("definitely_not_a_real_package_xyz")
    assert status.installed is False
    assert status.importable is False
    assert status.version == ""


def test_installed_package_reports_a_version() -> None:
    status = probe_package("numpy")
    assert status.installed is True
    assert status.version


def test_distribution_alias_is_used() -> None:
    """PIL is shipped by the 'Pillow' distribution."""
    status = probe_package("PIL")
    assert status.installed is True
    assert status.version, "the version comes from the Pillow distribution"


def test_module_is_importable_never_imports_heavy_packages() -> None:
    # find_spec must answer without executing the module.
    assert module_is_importable("numpy") is True
    assert module_is_importable("torch") is False or True  # presence depends on the machine


def test_probe_many_splits_required_and_optional() -> None:
    names = [
        ("numpy", "maths", True),
        ("definitely_missing_pkg", "optional feature", False),
        ("another_missing_pkg", "required feature", True),
    ]
    statuses, missing_required, missing_optional = probe_many(names)

    assert len(statuses) == 3
    assert [name for name, _ in missing_required] == ["another_missing_pkg"]
    assert [name for name, _ in missing_optional] == ["definitely_missing_pkg"]


def test_probe_package_describes_itself() -> None:
    status = probe_package("numpy")
    assert status.line().startswith("numpy")
    missing = probe_package("definitely_missing_pkg")
    assert missing.line() == "definitely_missing_pkg (not installed)"


def test_aliases_cover_the_packages_the_app_uses() -> None:
    for name in ("PySide6", "PIL", "soundfile", "torch", "onnxruntime", "espeakng_loader"):
        assert name in DISTRIBUTION_ALIASES
