"""Opening a user bundle for import: every check runs before anything is read for use.

:func:`open_bundle` refuses (:class:`BundleRefused`, with a message for the
administrator) an archive that:

* is larger than :attr:`Limits.max_bundle_bytes`, or is not a zip archive;
* has more than :attr:`Limits.max_members` members, unpacks to more than
  :attr:`Limits.max_unpacked_bytes`, or has a member that expands more than
  :attr:`Limits.max_ratio` times (a zip bomb);
* has a member named outside the layout of :mod:`spark.bundles.format` (an
  absolute path, a parent component, a backslash, a hidden or device name, an
  unknown table), a member that is a link or another non-regular file, an
  encrypted member, or the same member twice;
* has no readable manifest, another format, a ``format_version`` other than
  :data:`~spark.bundles.format.FORMAT_VERSION`, or a bad bundle id;
* lists members it lacks or lacks members it has, or has a member whose size or
  sha256 differs from the manifest (a file's content must also match its name);
* holds anything shaped like a credential (:mod:`spark.bundles.secrets`).

Nothing is ever extracted to disk: members are read from the archive, and an
import writes skill files and file contents through the file area, which
contains every key again.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from spark.bundles import format as bf
from spark.bundles import secrets

__all__ = ["Bundle", "BundleRefused", "Limits", "open_bundle"]

_MB = 1 << 20
_DATA = re.compile(r"^data/([a-z_]+)\.jsonl$")
_FILE = re.compile(r"^files/([0-9a-f]{64})$")
_BUNDLE_ID = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_MAX = 16 * _MB
_JSON_MAX = 16 * _MB
# Members smaller than this are never refused for their compression ratio.
_RATIO_FLOOR = _MB


class BundleRefused(Exception):
    """The bundle is not imported; the message says why (and never quotes content)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """What an import accepts (the size limit, A17)."""

    max_bundle_bytes: int = 1024 * _MB
    max_unpacked_bytes: int = 4096 * _MB
    max_members: int = 200_000
    max_ratio: int = 200


def _valid_name(name: str) -> bool:
    if name in (bf.MANIFEST, bf.PREFERENCES):
        return True
    data = _DATA.match(name)
    if data:
        return data.group(1) in bf.TABLES
    if _FILE.match(name):
        return True
    parts = name.split("/", 2)
    if len(parts) == 3 and parts[0] == "skills":
        return bool(bf.SKILL_NAME.match(parts[1])) and bf.skill_path_parts(parts[2]) is not None
    return False


def _regular(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    if info.create_system != 3 or mode == 0:
        return not info.is_dir()
    return stat.S_ISREG(mode)


class Bundle:
    """An opened, checked bundle. Use as a context manager; it holds the archive open."""

    def __init__(self, archive: zipfile.ZipFile, manifest: dict[str, Any]) -> None:
        self._zip = archive
        self.manifest = manifest

    @property
    def bundle_id(self) -> str:
        return str(self.manifest["bundle_id"])

    def _read(self, name: str) -> bytes:
        try:
            return self._zip.read(name)
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise BundleRefused(f"the bundle member {name} is damaged") from exc

    def rows(self, table: str) -> Iterator[dict[str, Any]]:
        """``table``'s rows, values decoded; refuses a row that is not one the format allows."""
        member = bf.data_member(table)
        if member not in self.manifest["members"]:
            return
        columns = set(bf.TABLES[table])
        for number, line in enumerate(self._read(member).decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise BundleRefused(f"{member} row {number} is not JSON") from None
            if not isinstance(row, dict):
                raise BundleRefused(f"{member} row {number} is not an object")
            unknown = set(row) - columns
            if unknown:
                raise BundleRefused(f"{member} row {number} has a column the format does not allow")
            try:
                yield {k: bf.decode_value(v) for k, v in row.items()}
            except ValueError:
                raise BundleRefused(f"{member} row {number} has a value it cannot read") from None

    def blob(self, sha256: str) -> bytes:
        """The content of a file, by its sha256; refused when the bundle lacks it."""
        name = f"files/{sha256}"
        if not _FILE.match(name) or name not in self.manifest["members"]:
            raise BundleRefused("a file row names content the bundle does not have")
        return self._read(name)

    def skill_files(self) -> list[tuple[str, str, bytes]]:
        """Every skill file as ``(skill, path inside the skill, content)``, sorted."""
        out = []
        for name in sorted(self.manifest["members"]):
            if name.startswith("skills/"):
                _, skill, relative = name.split("/", 2)
                out.append((skill, relative, self._read(name)))
        return out

    def preferences(self) -> dict[str, Any]:
        if bf.PREFERENCES not in self.manifest["members"]:
            return {}
        try:
            value = json.loads(self._read(bf.PREFERENCES))
        except ValueError:
            raise BundleRefused("the preferences are not JSON") from None
        if not isinstance(value, dict):
            raise BundleRefused("the preferences are not an object")
        return value

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Bundle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _amount(nbytes: int) -> str:
    return f"{nbytes // _MB} MB" if nbytes >= _MB else f"{nbytes} bytes"


def _size_of(source: IO[bytes]) -> int:
    position = source.tell()
    size = source.seek(0, 2)
    source.seek(position)
    return size


def _check_infos(infos: list[zipfile.ZipInfo], limits: Limits) -> None:
    if len(infos) > limits.max_members:
        raise BundleRefused(f"the bundle has more than {limits.max_members} members")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name in seen:
            raise BundleRefused(f"the bundle holds {name!r} more than once")
        seen.add(name)
        if not _valid_name(name):
            raise BundleRefused(f"the bundle has a member name outside its layout: {name!r}")
        if info.flag_bits & 0x1:
            raise BundleRefused(f"the bundle member {name} is encrypted")
        if not _regular(info):
            raise BundleRefused(f"the bundle member {name} is not a regular file")
        total += info.file_size
        if total > limits.max_unpacked_bytes:
            raise BundleRefused(
                f"the bundle's unpacked size exceeds {_amount(limits.max_unpacked_bytes)}"
            )
        if info.file_size > _RATIO_FLOOR and info.file_size > limits.max_ratio * max(
            info.compress_size, 1
        ):
            raise BundleRefused(f"the bundle member {name} exceeds the compression limit")


def _manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        info = archive.getinfo(bf.MANIFEST)
    except KeyError:
        raise BundleRefused("the bundle has no manifest") from None
    if info.file_size > _MANIFEST_MAX:
        raise BundleRefused("the bundle manifest is too large")
    try:
        raw = archive.read(info)
        doc = json.loads(raw)
    except (ValueError, zipfile.BadZipFile, OSError, EOFError):
        raise BundleRefused("the bundle manifest cannot be read") from None
    found = secrets.find(raw)
    if found:
        raise BundleRefused(
            f"the bundle manifest holds what looks like a credential ({', '.join(found)}); "
            "bundles never carry secrets"
        )
    if not isinstance(doc, dict):
        raise BundleRefused("the bundle manifest cannot be read")
    if doc.get("format") != bf.FORMAT:
        raise BundleRefused("this is not a Spark user bundle")
    version = doc.get("format_version")
    if type(version) is not int or version != bf.FORMAT_VERSION:
        raise BundleRefused(
            f"the bundle format version is {version!r}; this Spark reads version "
            f"{bf.FORMAT_VERSION} only"
        )
    if not isinstance(doc.get("bundle_id"), str) or not _BUNDLE_ID.match(doc["bundle_id"]):
        raise BundleRefused("the bundle id is not valid")
    members = doc.get("members")
    if not isinstance(members, dict) or not all(
        isinstance(v, dict) and type(v.get("size")) is int and isinstance(v.get("sha256"), str)
        for v in members.values()
    ):
        raise BundleRefused("the bundle manifest has no valid member table")
    return doc


def _verify(archive: zipfile.ZipFile, doc: dict[str, Any]) -> None:
    listed = set(doc["members"])
    present = {i.filename for i in archive.infolist()} - {bf.MANIFEST}
    for name in sorted(present - listed):
        raise BundleRefused(f"the bundle member {name} is not listed in the manifest")
    for name in sorted(listed - present):
        raise BundleRefused(f"the bundle member {name} is missing")
    for name in sorted(present):
        expected = doc["members"][name]
        digest = hashlib.sha256()
        size = 0
        found: set[str] = set()
        try:
            with archive.open(name) as stream:
                hashing = _Hashing(stream, digest)
                found.update(secrets.find_in_stream(hashing))
                size = hashing.size
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise BundleRefused(f"the bundle member {name} is damaged") from exc
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise BundleRefused(f"the bundle member {name} does not match the manifest")
        blob = _FILE.match(name)
        if blob and blob.group(1) != expected["sha256"]:
            raise BundleRefused(f"the content of {name} does not match its name")
        if found:
            raise BundleRefused(
                f"the bundle member {name} holds what looks like a credential "
                f"({', '.join(sorted(found))}); bundles never carry secrets"
            )
    for name in (bf.PREFERENCES,):
        if name in present and doc["members"][name]["size"] > _JSON_MAX:
            raise BundleRefused(f"the bundle member {name} is too large")


class _Hashing:
    """A read-through stream that hashes and counts what passes."""

    def __init__(self, stream: IO[bytes], digest: Any) -> None:
        self._stream = stream
        self._digest = digest
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        data = self._stream.read(n)
        self._digest.update(data)
        self.size += len(data)
        return data


def open_bundle(source: IO[bytes] | Path, limits: Limits) -> Bundle:
    """Open and check a bundle (see the module docstring); raises :class:`BundleRefused`."""
    size = source.stat().st_size if isinstance(source, Path) else _size_of(source)
    if size > limits.max_bundle_bytes:
        raise BundleRefused(
            f"the bundle is larger than {_amount(limits.max_bundle_bytes)}, the import limit"
        )
    try:
        # Given a path, the archive owns (and closes) its file.
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError, EOFError):
        raise BundleRefused("the upload is not a zip archive") from None
    try:
        _check_infos(archive.infolist(), limits)
        doc = _manifest(archive)
        _verify(archive, doc)
    except BaseException:
        archive.close()
        raise
    return Bundle(archive, doc)
