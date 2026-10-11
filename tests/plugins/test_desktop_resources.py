"""Package resource validation for desktop plugin assets."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from tongs.plugins.desktop import (
    DesktopAsset,
    DesktopAssetBundle,
    DesktopAssetKind,
    DesktopCompatibility,
    DesktopModule,
    DesktopPluginManifest,
)
from tongs.plugins.desktop_resources import (
    find_asset,
    validate_asset_resources,
    validate_resource_containment,
)


def resource_manifest(package: str, root: str = "assets") -> DesktopPluginManifest:
    return DesktopPluginManifest(
        plugin_id="resource",
        title="Resource",
        version="1.0",
        compatibility=DesktopCompatibility(1),
        modules=(DesktopModule("main", "Main", "ui", "main"),),
        asset_bundles=(
            DesktopAssetBundle(
                "ui",
                package,
                root,
                (DesktopAsset("main", "main.mjs", DesktopAssetKind.MODULE),),
            ),
        ),
    )


def make_package(root: Path, name: str, asset_root: str = "assets") -> Path:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / asset_root
    target.mkdir(parents=True)
    (target / "main.mjs").write_text("export const ready = true;", encoding="utf-8")
    importlib.invalidate_caches()
    return package


def test_installed_package_resources_return_logical_descriptors(
    desktop_fixture_root: Path,
) -> None:
    assets = validate_asset_resources(resource_manifest("fixture_desktop_assets"))

    assert len(assets) == 1
    assert assets[0].package == "fixture_desktop_assets"
    assert assets[0].path == "main.mjs"
    assert assets[0].media_type == "text/javascript; charset=utf-8"
    assert assets[0].byte_count > 0
    assert not hasattr(assets[0], "absolute_path")


def test_bundle_root_symlink_outside_package_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "root_link_fixture", "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.mjs").write_text("export {};", encoding="utf-8")
    (package / "inside" / "main.mjs").unlink()
    (package / "inside").rmdir()
    (package / "assets").symlink_to(outside, target_is_directory=True)
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="symlink"):
        validate_asset_resources(resource_manifest("root_link_fixture"))


@pytest.mark.parametrize("root_parts", [("..",), ("assets", "..", "..")])
def test_bundle_root_outside_package_is_rejected_without_asset_check(
    tmp_path: Path, root_parts: tuple[str, ...]
) -> None:
    package = make_package(tmp_path, "root_escape_fixture")

    with pytest.raises(ValueError, match="escapes its declared package root"):
        validate_resource_containment(
            package, package.joinpath(*root_parts), root_parts
        )


def test_bundle_root_link_is_rejected_for_a_bundle_without_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no assets, no per-asset check runs, so only the bundle root's own
    # containment check stands between the package and the outside folder.
    package = make_package(tmp_path, "empty_bundle_link_fixture")
    outside = tmp_path / "empty_bundle_outside"
    outside.mkdir()
    (package / "extra").symlink_to(outside, target_is_directory=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("empty_bundle_link_fixture")
    manifest = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            *base.asset_bundles,
            DesktopAssetBundle("extra", "empty_bundle_link_fixture", "extra", ()),
        ),
    )

    with pytest.raises(ValueError, match="symlink"):
        validate_asset_resources(manifest)


def test_bundle_root_symlinked_ancestor_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "ancestor_link_fixture", "placeholder")
    outside = tmp_path / "outside_parent"
    (outside / "assets").mkdir(parents=True)
    (outside / "assets" / "main.mjs").write_text("export {};", encoding="utf-8")
    (package / "nested").symlink_to(outside, target_is_directory=True)
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="symlink"):
        validate_asset_resources(
            resource_manifest("ancestor_link_fixture", "nested/assets")
        )


def test_asset_file_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "file_link_fixture")
    outside = tmp_path / "outside.mjs"
    outside.write_text("export {};", encoding="utf-8")
    (package / "assets" / "main.mjs").unlink()
    (package / "assets" / "main.mjs").symlink_to(outside)
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="symlink"):
        validate_asset_resources(resource_manifest("file_link_fixture"))

    sys.modules.pop("file_link_fixture", None)


def test_configured_file_size_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_package(tmp_path, "size_limit_fixture")
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("size_limit_fixture")
    bundle = base.asset_bundles[0]
    limited = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            DesktopAssetBundle(
                bundle.id,
                bundle.package,
                bundle.root,
                bundle.assets,
                max_file_bytes=1,
                max_total_bytes=1,
            ),
        ),
    )

    with pytest.raises(ValueError, match="per-file size limit"):
        validate_asset_resources(limited)


def test_duplicate_normalized_resource_paths_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "duplicate_path_fixture")
    (package / "assets" / "MAIN.MJS").write_text("export {};", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("duplicate_path_fixture")
    bundle = base.asset_bundles[0]
    duplicate = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            DesktopAssetBundle(
                bundle.id,
                bundle.package,
                bundle.root,
                (
                    *bundle.assets,
                    DesktopAsset("alternate", "MAIN.MJS", DesktopAssetKind.MODULE),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="Duplicate normalized asset path"):
        validate_asset_resources(duplicate)


def test_namespace_package_bundle_root_link_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    package_one = first / "namespace_asset_fixture"
    package_two = second / "namespace_asset_fixture"
    package_one.mkdir(parents=True)
    package_two.mkdir(parents=True)
    outside = tmp_path / "namespace_outside"
    outside.mkdir()
    (outside / "main.mjs").write_text("export {};", encoding="utf-8")
    (package_one / "assets").symlink_to(outside, target_is_directory=True)
    (package_two / "assets").mkdir()
    monkeypatch.syspath_prepend(str(first))
    monkeypatch.syspath_prepend(str(second))
    importlib.invalidate_caches()

    try:
        with pytest.raises(ValueError, match="symlink"):
            validate_asset_resources(resource_manifest("namespace_asset_fixture"))
    finally:
        sys.modules.pop("namespace_asset_fixture", None)


def test_find_asset_rejects_unknown_asset(
    desktop_fixture_root: Path,
) -> None:
    assets = validate_asset_resources(resource_manifest("fixture_desktop_assets"))

    with pytest.raises(LookupError, match="Unknown or ambiguous"):
        find_asset(assets, "resource", "missing")


def test_bundle_total_size_limit_across_multiple_files_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "total_size_fixture")
    (package / "assets" / "main.mjs").write_text("x" * 600, encoding="utf-8")
    (package / "assets" / "second.mjs").write_text("y" * 600, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("total_size_fixture")
    bundle = base.asset_bundles[0]
    limited = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            DesktopAssetBundle(
                bundle.id,
                bundle.package,
                bundle.root,
                (
                    *bundle.assets,
                    DesktopAsset("second", "second.mjs", DesktopAssetKind.MODULE),
                ),
                max_file_bytes=1000,
                max_total_bytes=1000,
            ),
        ),
    )

    with pytest.raises(ValueError, match="exceeds total size limit"):
        validate_asset_resources(limited)


def test_directory_declared_as_asset_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "dir_asset_fixture")
    (package / "assets" / "subdir.mjs").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("dir_asset_fixture")
    bundle = base.asset_bundles[0]
    manifest = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            DesktopAssetBundle(
                bundle.id,
                bundle.package,
                bundle.root,
                (
                    *bundle.assets,
                    DesktopAsset("subdir", "subdir.mjs", DesktopAssetKind.MODULE),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="not a regular file"):
        validate_asset_resources(manifest)


def test_file_size_accumulated_across_chunks_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = make_package(tmp_path, "chunk_size_fixture")
    large_content = "x" * (70 * 1024)
    (package / "assets" / "main.mjs").write_text(large_content, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    base = resource_manifest("chunk_size_fixture")
    bundle = base.asset_bundles[0]
    limited = DesktopPluginManifest(
        plugin_id=base.plugin_id,
        title=base.title,
        version=base.version,
        compatibility=base.compatibility,
        modules=base.modules,
        asset_bundles=(
            DesktopAssetBundle(
                bundle.id,
                bundle.package,
                bundle.root,
                bundle.assets,
                max_file_bytes=65 * 1024,
                max_total_bytes=100 * 1024,
            ),
        ),
    )

    with pytest.raises(ValueError, match="per-file size limit"):
        validate_asset_resources(limited)
