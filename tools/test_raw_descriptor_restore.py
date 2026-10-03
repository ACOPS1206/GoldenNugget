#!/usr/bin/env python3
"""Offline regression tests for iOS 26 raw descriptor recovery."""
import os
import plistlib
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.tweaks.posterboard.raw_descriptor_tendie import (
    COLLECTIONS_PROVIDER, MERCURY_PROVIDER, RawDescriptorTendie,
)
from src.tweaks.posterboard.exact_configuration_tendie import ExactConfigurationTendie


def make_tendie(path, provider=COLLECTIONS_PROVIDER, root="descriptors"):
    metadata = plistlib.dumps({
        "extensionBundleIdentifier": provider,
        "descriptorIdentifier": "7565.DYNAMIC",
    })
    original = plistlib.dumps({"identifier": 7920, "family": "Marble"})
    with zipfile.ZipFile(path, "w") as archive:
        base = f"{root}/ORIGINAL-DESCRIPTOR"
        archive.writestr(
            f"{base}/com.apple.posterkit.provider.identifierURL."
            "suggestionMetadata.plist", metadata)
        archive.writestr(
            f"{base}/com.apple.posterkit.provider.descriptor.identifier", b"7565")
        archive.writestr(
            f"{base}/versions/0/contents/"
            ".com.apple.posterkit.provider.contents.configurableOptions.plist",
            b"meaningful-dotfile")
        archive.writestr(
            f"{base}/versions/0/contents/7920.Marble.wallpaper/Wallpaper.plist",
            original)
    return original


def apply_raw(tendie):
    raw = RawDescriptorTendie(tendie)
    with tempfile.TemporaryDirectory() as output:
        files = raw.build_restore_files(output)
        snapshots = [(item.restore_path,
                      open(item.contents_path, "rb").read()) for item in files]
    return snapshots


with tempfile.TemporaryDirectory() as tmp:
    valid = os.path.join(tmp, "valid.tendies")
    original = make_tendie(valid)
    restored = apply_raw(valid)
    paths = [path for path, _ in restored]

    assert len(restored) == 4
    assert all(f"/{COLLECTIONS_PROVIDER}/descriptors/ORIGINAL-DESCRIPTOR/" in path
               for path in paths)
    assert not any("/configurations/" in path for path in paths)
    assert not any("PBFPosterExtensionDataStoreSQLiteDatabase" in path
                   for path in paths)
    wallpaper = next(data for path, data in restored
                     if path.endswith("Wallpaper.plist"))
    assert wallpaper == original
    assert plistlib.loads(wallpaper)["identifier"] == 7920
    assert any(data == b"7565" for _, data in restored)
    assert any(data == b"meaningful-dotfile" for _, data in restored)

    mercury = os.path.join(tmp, "mercury.tendies")
    make_tendie(mercury, provider=MERCURY_PROVIDER)
    mercury_restored = apply_raw(mercury)
    assert all(f"/{MERCURY_PROVIDER}/descriptors/ORIGINAL-DESCRIPTOR/" in path
               for path, _ in mercury_restored)

    exact = os.path.join(tmp, "exact.tendies")
    descriptor_uuid = "4C654322-41FA-452D-A463-8E28925A378D"
    configuration_uuid = "C5EB18DA-A31A-42A8-8244-B37EE42E9427"
    metadata = plistlib.dumps({"provider": MERCURY_PROVIDER})
    with zipfile.ZipFile(exact, "w") as archive:
        for root, item in (("descriptors", descriptor_uuid),
                           ("configurations", configuration_uuid)):
            base = f"{root}/{item}"
            archive.writestr(f"{base}/com.apple.posterkit.provider.identifierURL."
                             "suggestionMetadata.plist", metadata)
            archive.writestr(
                f"{base}/com.apple.posterkit.provider.descriptor.identifier", b"v5x")
            archive.writestr(f"{base}/versions/0/contents/payload", root.encode())
    recovered = ExactConfigurationTendie(exact)
    assert recovered.descriptor_name == descriptor_uuid
    assert recovered.configuration_uuid == configuration_uuid
    with tempfile.TemporaryDirectory() as output:
        exact_files = recovered.build_restore_files(output)
        exact_paths = [item.restore_path for item in exact_files]
    assert any(f"/{MERCURY_PROVIDER}/descriptors/{descriptor_uuid}/" in p
               for p in exact_paths)
    assert any(f"/{MERCURY_PROVIDER}/configurations/{configuration_uuid}/" in p
               for p in exact_paths)
    assert not any("PBFPosterExtensionDataStoreSQLiteDatabase" in p
                   for p in exact_paths)

    unsupported = os.path.join(tmp, "unsupported.tendies")
    make_tendie(unsupported, provider="com.example.UnsupportedPoster")
    try:
        RawDescriptorTendie(unsupported)
        raise AssertionError("Unsupported provider was accepted")
    except Exception as exc:
        assert "supported provider" in str(exc)

    container = os.path.join(tmp, "container.tendies")
    make_tendie(container, root="container")
    try:
        RawDescriptorTendie(container)
        raise AssertionError("container archive was accepted")
    except Exception as exc:
        assert "descriptor/descriptors root" in str(exc)

    traversal = os.path.join(tmp, "traversal.tendies")
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("descriptors/../escape", b"nope")
    try:
        RawDescriptorTendie(traversal)
        raise AssertionError("path traversal archive was accepted")
    except Exception as exc:
        assert "Unsafe path" in str(exc)

print("raw descriptor restore checks passed")
