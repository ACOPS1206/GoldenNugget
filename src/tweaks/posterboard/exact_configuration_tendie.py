"""Original PosterBoard descriptor and configuration registration archives.

Unlike normal tendies, these archives preserve the descriptor/configuration
folder UUIDs and every file byte.  They contain one matching provider pair and
no database file; registration is merged into
the freshly fetched on-device database by :class:`PBConfigManager`.
"""
from __future__ import annotations

import os
import plistlib
import re
import stat
import zipfile
from pathlib import PurePosixPath

from src.exceptions.nugget_exception import NuggetException
from src.utils.file_to_restore import FileToRestore

from .raw_descriptor_tendie import (
    ALLOWED_PROVIDERS, RAW_STRUCTURE_VERSION, _plist_strings,
)


_ROOTS = {"descriptors", "configurations"}
_METADATA = "com.apple.posterkit.provider.identifierURL.suggestionMetadata.plist"
_IDENTIFIER = "com.apple.posterkit.provider.descriptor.identifier"
_UUID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


class ExactConfigurationTendie:
    """One byte-preserved descriptor and configuration for one known provider."""

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path)
        self.provider = ""
        self.descriptor_name = ""
        self.configuration_uuid = ""
        self._members: list[tuple[str, str, str]] = []
        self._validate()

    @staticmethod
    def _parts(name: str) -> tuple[str, ...]:
        if "\\" in name:
            raise NuggetException("Exact recovery archives must use ZIP-style paths.")
        value = PurePosixPath(name)
        if value.is_absolute() or ".." in value.parts:
            raise NuggetException("Unsafe path found in exact recovery archive.")
        return value.parts

    def _validate(self):
        names = {root: set() for root in _ROOTS}
        metadata = {}
        identifiers = {}
        seen = set()
        try:
            archive = zipfile.ZipFile(self.path, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise NuggetException(f"Invalid exact recovery archive: {exc}") from exc

        with archive:
            for info in archive.infolist():
                parts = self._parts(info.filename)
                if not parts:
                    continue
                if (parts[0].lower() == "__macosx" or
                        any(p == ".DS_Store" or p.startswith("._") for p in parts)):
                    continue
                root = parts[0].lower()
                if root not in _ROOTS:
                    raise NuggetException(
                        "Exact recovery accepts only descriptors and configurations roots.")
                if len(parts) < 2:
                    continue
                item_name = parts[1]
                names[root].add(item_name)
                if len(names[root]) > 1:
                    raise NuggetException(
                        "Registration accepts exactly one descriptor and one configuration.")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise NuggetException("Symbolic links are not allowed in exact recovery archives.")
                if info.is_dir():
                    continue
                relative = PurePosixPath(*parts[1:]).as_posix()
                key = (root, relative.casefold())
                if key in seen:
                    raise NuggetException(f"Duplicate exact recovery path: {root}/{relative}")
                seen.add(key)
                self._members.append((info.filename, root, relative))
                if len(parts) == 3 and parts[2] == _METADATA:
                    try:
                        metadata[(root, item_name)] = set(
                            _plist_strings(plistlib.loads(archive.read(info))))
                    except Exception as exc:
                        raise NuggetException(
                            f"Invalid suggestion metadata for {item_name}: {exc}") from exc
                if len(parts) == 3 and parts[2] == _IDENTIFIER:
                    identifiers[(root, item_name)] = archive.read(info)

        if len(names["descriptors"]) != 1 or len(names["configurations"]) != 1:
            raise NuggetException(
                "Registration requires one descriptor and one configuration.")
        descriptor = next(iter(names["descriptors"]))
        configuration = next(iter(names["configurations"]))
        if not _UUID_RE.fullmatch(descriptor) or not _UUID_RE.fullmatch(configuration):
            raise NuggetException("Descriptor and configuration folder names must be original UUIDs.")
        providers = []
        for key in (("descriptors", descriptor), ("configurations", configuration)):
            matches = ALLOWED_PROVIDERS.intersection(metadata.get(key, set()))
            if len(matches) != 1:
                raise NuggetException(
                    f"{key[1]} must identify exactly one supported PosterBoard provider.")
            providers.append(matches.pop())
        if providers[0] != providers[1]:
            raise NuggetException(
                "Descriptor and configuration belong to different providers.")
        descriptor_id = identifiers.get(("descriptors", descriptor))
        configuration_id = identifiers.get(("configurations", configuration))
        if not descriptor_id or descriptor_id != configuration_id:
            raise NuggetException(
                "Descriptor and configuration identifiers are missing or do not match.")
        self.descriptor_name = descriptor
        self.configuration_uuid = configuration
        self.provider = providers[0]

    def build_restore_files(self, output_dir: str) -> list[FileToRestore]:
        result = []
        with zipfile.ZipFile(self.path, "r") as archive:
            for archive_name, root, relative in self._members:
                local = os.path.join(output_dir, root, *PurePosixPath(relative).parts)
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with archive.open(archive_name) as source, open(local, "wb") as dest:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        dest.write(chunk)
                restore_root = (
                    "/Library/Application Support/PRBPosterExtensionDataStore/"
                    f"{RAW_STRUCTURE_VERSION}/Extensions/{self.provider}/{root}")
                result.append(FileToRestore(
                    contents=None,
                    contents_path=local,
                    restore_path=f"{restore_root}/{relative}",
                    domain="AppDomain-com.apple.PosterBoard",
                ))
        return result
