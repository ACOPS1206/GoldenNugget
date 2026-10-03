"""Validated, byte-preserving PosterBoard descriptor archives.

This is deliberately separate from :mod:`tendie_file`: normal tendies are
converted to configurations and have their identifiers randomized.  The raw
restore path is a narrowly-scoped recovery tool and must never inherit that
behaviour.
"""
from __future__ import annotations

import os
import plistlib
import stat
import zipfile
from pathlib import PurePosixPath

from src.exceptions.nugget_exception import NuggetException
from src.utils.file_to_restore import FileToRestore


COLLECTIONS_PROVIDER = "com.apple.WallpaperKit.CollectionsPoster"
MERCURY_PROVIDER = "com.apple.MercuryPoster"
PHOTOS_PROVIDER = "com.apple.PhotosUIPrivate.PhotosPosterProvider"
ALLOWED_PROVIDERS = {COLLECTIONS_PROVIDER, MERCURY_PROVIDER, PHOTOS_PROVIDER}
RAW_STRUCTURE_VERSION = 61
_DESCRIPTOR_ROOTS = {"descriptor", "descriptors"}
_SUGGESTION_METADATA = (
    "com.apple.posterkit.provider.identifierURL.suggestionMetadata.plist"
)


def _plist_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _plist_strings(key)
            yield from _plist_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _plist_strings(item)


class RawDescriptorTendie:
    """A validated descriptor-only tendie for supported PosterBoard providers."""

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path)
        self._members: list[tuple[str, str]] = []
        self.descriptor_names: list[str] = []
        self.provider: str | None = None
        self._validate()

    @property
    def descriptor_cnt(self) -> int:
        return len(self.descriptor_names)

    @staticmethod
    def _parts(name: str) -> tuple[str, ...]:
        if "\\" in name:
            raise NuggetException("Raw descriptor archives must use ZIP-style paths.")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise NuggetException("Unsafe path found in raw descriptor archive.")
        return path.parts

    def _validate(self):
        descriptor_names = set()
        metadata_by_descriptor = {}
        seen_destinations = set()

        try:
            archive = zipfile.ZipFile(self.path, mode="r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise NuggetException(f"Invalid tendies archive: {exc}") from exc

        with archive:
            for info in archive.infolist():
                parts = self._parts(info.filename)
                if not parts:
                    continue
                if (parts[0].lower() == "__macosx"
                        or any(part == ".DS_Store" or part.startswith("._")
                               for part in parts)):
                    continue
                if parts[0].lower() not in _DESCRIPTOR_ROOTS:
                    raise NuggetException(
                        "Raw restore accepts only a descriptor/descriptors root; "
                        "container and provider-specific archives are not allowed.")
                if len(parts) < 2:
                    continue

                descriptor_name = parts[1]
                descriptor_names.add(descriptor_name)
                if len(descriptor_names) > 10:
                    raise NuggetException(
                        "Raw restore accepts at most 10 PosterBoard descriptors.")

                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise NuggetException("Symbolic links are not allowed in raw descriptors.")
                if info.is_dir():
                    continue

                relative = PurePosixPath(*parts[1:]).as_posix()
                destination_key = relative.casefold()
                if destination_key in seen_destinations:
                    raise NuggetException(
                        f"Duplicate raw descriptor path: {relative}")
                seen_destinations.add(destination_key)
                self._members.append((info.filename, relative))

                if len(parts) == 3 and parts[2] == _SUGGESTION_METADATA:
                    try:
                        metadata = plistlib.loads(archive.read(info))
                    except Exception as exc:
                        raise NuggetException(
                            f"Invalid suggestion metadata for {descriptor_name}: {exc}") from exc
                    metadata_by_descriptor[descriptor_name] = set(
                        _plist_strings(metadata))

        if not descriptor_names or not self._members:
            raise NuggetException("No descriptors were found in this tendies file.")

        provider_by_descriptor = {}
        for descriptor_name in sorted(descriptor_names):
            strings = metadata_by_descriptor.get(descriptor_name, set())
            matches = ALLOWED_PROVIDERS.intersection(strings)
            if len(matches) != 1:
                allowed = ", ".join(sorted(ALLOWED_PROVIDERS))
                raise NuggetException(
                    f"{descriptor_name} is not verified as exactly one supported "
                    f"provider ({allowed}). Raw restore was not scheduled.")
            provider_by_descriptor[descriptor_name] = matches.pop()

        providers = set(provider_by_descriptor.values())
        if len(providers) != 1:
            raise NuggetException(
                "A raw descriptor archive cannot mix PosterBoard providers.")
        self.provider = providers.pop()
        self.descriptor_names = sorted(descriptor_names)

    def extract_files(self, output_dir: str) -> list[tuple[str, str]]:
        """Extract validated files and return ``(local_path, relative_path)``.

        File bytes and all archive path components below ``descriptors/`` are
        preserved. Finder metadata is intentionally omitted, while meaningful
        dotfiles such as configurable-options plists are retained.
        """
        extracted = []
        with zipfile.ZipFile(self.path, mode="r") as archive:
            for archive_name, relative in self._members:
                local_path = os.path.join(output_dir, *PurePosixPath(relative).parts)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with archive.open(archive_name) as source, open(local_path, "wb") as dest:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        dest.write(chunk)
                extracted.append((local_path, relative))
        return extracted

    def build_restore_files(self, output_dir: str) -> list[FileToRestore]:
        """Build the standard restore-pipeline payload for this archive."""
        restore_root = (
            "/Library/Application Support/PRBPosterExtensionDataStore/"
            f"{RAW_STRUCTURE_VERSION}/Extensions/{self.provider}/descriptors"
        )
        return [
            FileToRestore(
                contents=None,
                contents_path=local_path,
                restore_path=f"{restore_root}/{relative_path}",
                domain="AppDomain-com.apple.PosterBoard",
            )
            for local_path, relative_path in self.extract_files(output_dir)
        ]
