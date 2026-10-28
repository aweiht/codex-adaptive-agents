#!/usr/bin/env python3
"""Build and verify the public 0.9.0 Python source/runtime ZIP.

The builder deliberately works from an explicit manifest. It never walks the
repository as an implicit release allowlist, follows links, or replaces an
existing archive.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = PROJECT_ROOT / "VERSION"
MANIFEST_FILE = PROJECT_ROOT / "release-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS"
EXPECTED_VERSION = "0.9.0"
PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|Volumes|private/var|home)/[^\s<>)\"']+"
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")
CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})  (.+)$")
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
HAS_DIR_FD_OPEN = os.open in getattr(os, "supports_dir_fd", set())
WINDOWS_STAT = os.name == "nt"


@dataclass(frozen=True)
class SourceSnapshot:
    """Bytes and metadata read from one validated regular-file identity."""

    data: bytes
    mode: int
    sha256: str
    identity: tuple[int, int, int, int, int, int]
    parent_identity: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class SourceCandidate:
    path: Path
    info: os.stat_result
    parent_identity: tuple[tuple[int, int], ...]


class ReleaseError(RuntimeError):
    """A release input or output failed an auditable package check."""


def _duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ReleaseError(f"non-finite JSON number: {value}")


def read_json(path: Path) -> Any:
    try:
        return json.loads(
            _read_regular_file(path).decode("utf-8"),
            object_pairs_hook=_duplicate_key,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read strict JSON {path}: {exc}") from exc


def safe_relative(value: str) -> str:
    """Return a normalized relative POSIX path or reject it."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleaseError(f"invalid relative path: {value!r}")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ReleaseError(f"non-portable or absolute path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReleaseError(f"path traversal or empty component: {value!r}")
    return "/".join(parts)


def safe_pattern(value: str) -> str:
    """Validate a manifest glob without expanding or normalizing its glob."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleaseError(f"invalid manifest glob: {value!r}")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ReleaseError(f"non-portable or absolute glob: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReleaseError(f"unsafe manifest glob: {value!r}")
    return value


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def has_forbidden_component(relative: str) -> bool:
    parts = relative.split("/")
    return any(
        part == "__pycache__"
        or part.endswith(".pyc")
        or (part.startswith(".") and part not in (".gitignore", ".github"))
        for part in parts
    )


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _path_handle_identity_matches(path_info: os.stat_result, handle_info: os.stat_result) -> bool:
    """Compare a checked path with its opened handle across Windows stat APIs.

    Windows may report different ``st_ctime_ns`` values from path stat and
    ``fstat`` even for the same file. When both APIs expose creation time, use
    that stable timestamp alongside the file index and content metadata.
    POSIX keeps the full identity check, including when descriptor-relative
    open is unavailable. Path/path and handle/handle comparisons always use
    the full identity.
    """
    if not WINDOWS_STAT:
        return _file_identity(path_info) == _file_identity(handle_info)

    path_birth = getattr(path_info, "st_birthtime_ns", None)
    handle_birth = getattr(handle_info, "st_birthtime_ns", None)
    if path_birth is None and handle_birth is None:
        timestamp_matches = int(path_info.st_ctime_ns) == int(handle_info.st_ctime_ns)
    elif path_birth is not None and handle_birth is not None:
        timestamp_matches = int(path_birth) == int(handle_birth)
    else:
        # The file ID still binds the path and handle. A missing creation-time
        # field cannot be substituted with Windows ctime, whose meaning can
        # differ between the two APIs.
        timestamp_matches = True

    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    return (timestamp_matches and
            all(int(getattr(path_info, field)) == int(getattr(handle_info, field))
                for field in stable_fields))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _checked_directory_chain(path: Path) -> tuple[tuple[int, int], ...]:
    """Reject symlinks/reparse points in every existing directory component."""
    path = _absolute(path)
    if not path.is_absolute():
        raise ReleaseError("absolute directory path required")
    current = Path(path.anchor)
    identities = []
    try:
        root_info = current.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot inspect directory path: {path}") from exc
    if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseError(f"unsafe directory path: {path}")
    identities.append((int(root_info.st_dev), int(root_info.st_ino)))
    for component in path.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ReleaseError(f"cannot inspect directory path: {path}") from exc
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"unsafe directory path: {current}")
        identities.append((int(info.st_dev), int(info.st_ino)))
    return tuple(identities)


def _open_directory_chain(
    path: Path,
    expected: tuple[tuple[int, int], ...] | None = None,
) -> tuple[int | None, tuple[tuple[int, int], ...]]:
    """Open a directory without following path components where dir_fd exists.

    Windows lacks dir_fd support in the Python standard library. There the
    caller uses _checked_directory_chain before and after its path-based open.
    """
    path = _absolute(path)
    observed = _checked_directory_chain(path)
    if expected is not None and observed != expected:
        raise ReleaseError(f"directory path changed after validation: {path}")
    expected_chain = expected if expected is not None else observed
    if os.name == "nt" or not HAS_DIR_FD_OPEN:
        return None, expected_chain
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise ReleaseError("secure directory-relative open is unavailable")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path.anchor, flags)
        identities = [_directory_identity(os.fstat(fd))]
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            identities.append(_directory_identity(os.fstat(fd)))
        info = os.fstat(fd)
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"unsafe directory path: {path}")
        opened = tuple(identities)
        if opened != expected_chain:
            raise ReleaseError(f"directory path changed during secure open: {path}")
        return fd, opened
    except Exception as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        if isinstance(exc, ReleaseError):
            raise
        if not isinstance(exc, OSError):
            raise
        raise ReleaseError(f"cannot securely open directory: {path}") from exc


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _open_regular_file(
    path: Path,
    *,
    expected: os.stat_result | None = None,
    expected_parent: tuple[tuple[int, int], ...] | None = None,
) -> tuple[int, os.stat_result, tuple[tuple[int, int], ...]]:
    """Open one regular file and bind the descriptor to its checked identity."""
    path = _absolute(path)
    parent = path.parent
    parent_before = _checked_directory_chain(parent)
    if expected_parent is not None and parent_before != expected_parent:
        raise ReleaseError(f"source parent changed after validation: {path}")
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot stat source file: {path}") from exc
    if _is_link_or_reparse(path_info) or not stat.S_ISREG(path_info.st_mode):
        raise ReleaseError(f"source path is not a regular file: {path}")
    expected_info = expected or path_info
    if _file_identity(path_info) != _file_identity(expected_info):
        raise ReleaseError(f"source file changed after validation: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    directory_fd, opened_parent = _open_directory_chain(parent, parent_before)
    fd: int | None = None
    try:
        if directory_fd is not None:
            fd = os.open(path.name, flags, dir_fd=directory_fd)
        else:
            # Windows has no Python dir_fd support. Open first, compare the
            # handle identity before reading, then recheck parent components.
            fd = os.open(path, flags)
        opened_info = os.fstat(fd)
        if (_is_link_or_reparse(opened_info) or not stat.S_ISREG(opened_info.st_mode)
                or not _path_handle_identity_matches(expected_info, opened_info)):
            raise ReleaseError(f"source file changed during secure open: {path}")
        if opened_parent != parent_before or _checked_directory_chain(parent) != parent_before:
            raise ReleaseError(f"source parent changed during secure open: {path}")
        result_fd = fd
        fd = None
        return result_fd, opened_info, parent_before
    except Exception as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if isinstance(exc, ReleaseError):
            raise
        if not isinstance(exc, OSError):
            raise
        raise ReleaseError(f"cannot securely open source file: {path}") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _read_regular_file(path: Path, *, expected: os.stat_result | None = None) -> bytes:
    fd, opened_info, parent_identity = _open_regular_file(path, expected=expected)
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
            after = os.fstat(handle.fileno())
            if _file_identity(after) != _file_identity(opened_info):
                raise ReleaseError(f"source file changed while reading: {path}")
            if _checked_directory_chain(path.parent) != parent_identity:
                raise ReleaseError(f"source parent changed while reading: {path}")
            return data
    except OSError as exc:
        raise ReleaseError(f"cannot read source file: {path}") from exc


def _source_lstat(path: Path, relative: str) -> os.stat_result:
    if not within(path, PROJECT_ROOT):
        raise ReleaseError(f"source path escapes repository: {relative}")
    current = PROJECT_ROOT
    for component in Path(relative).parts:
        current = current / component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ReleaseError(f"missing source path {relative}: {exc}") from exc
        if _is_link_or_reparse(info):
            raise ReleaseError(f"symlink is not allowed in source payload: {relative}")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot stat source path {relative}: {exc}") from exc
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"source path is not a regular file: {relative}")
    return info


def _source_candidate(path: Path, relative: str) -> SourceCandidate:
    """Stat a manifest member relative to an opened repository root."""
    relative = safe_relative(relative)
    parts = relative.split("/")
    root_identity = _checked_directory_chain(PROJECT_ROOT)
    if os.name != "nt" and HAS_DIR_FD_OPEN:
        directory_fd, opened_root = _open_directory_chain(PROJECT_ROOT, root_identity)
        assert directory_fd is not None
        identities = list(opened_root)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
            for component in parts[:-1]:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
                info = os.fstat(directory_fd)
                if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise ReleaseError(f"unsafe source directory: {relative}")
                identities.append(_directory_identity(info))
            info = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"source path is not a regular file: {relative}")
            return SourceCandidate(path, info, tuple(identities))
        except OSError as exc:
            raise ReleaseError(f"cannot securely inspect source path {relative}: {exc}") from exc
        finally:
            os.close(directory_fd)

    # Windows' stdlib lacks descriptor-relative stat/open. Recheck both the
    # file and the whole directory identity chain around lstat; the subsequent
    # opened-file fstat must match this candidate before any bytes are read.
    first_parent = _checked_directory_chain(path.parent)
    first_info = _source_lstat(path, relative)
    second_info = path.lstat()
    second_parent = _checked_directory_chain(path.parent)
    if (_file_identity(first_info) != _file_identity(second_info)
            or first_parent != second_parent):
        raise ReleaseError(f"source path changed during validation: {relative}")
    return SourceCandidate(path, second_info, second_parent)


def _snapshot_source(candidate: SourceCandidate, relative: str) -> SourceSnapshot:
    fd, opened_info, parent_identity = _open_regular_file(
        candidate.path,
        expected=candidate.info,
        expected_parent=candidate.parent_identity,
    )
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
            after = os.fstat(handle.fileno())
            if _file_identity(after) != _file_identity(opened_info):
                raise ReleaseError(f"source file changed while reading: {relative}")
        return SourceSnapshot(data=data, mode=stat.S_IMODE(opened_info.st_mode),
                              sha256=hashlib.sha256(data).hexdigest(),
                              identity=_file_identity(opened_info),
                              parent_identity=parent_identity)
    except OSError as exc:
        raise ReleaseError(f"cannot read source payload: {relative}") from exc


def _excluded(relative: str, excluded_paths: Iterable[str]) -> bool:
    normalized = relative.rstrip("/")
    for raw in excluded_paths:
        if not isinstance(raw, str) or not raw:
            raise ReleaseError(f"invalid excluded path: {raw!r}")
        candidate = raw.replace("\\", "/")
        if candidate.endswith("/"):
            if normalized == candidate[:-1] or normalized.startswith(candidate):
                return True
        elif normalized == candidate:
            return True
    return False


def load_manifest() -> dict[str, Any]:
    manifest = read_json(MANIFEST_FILE)
    if not isinstance(manifest, dict):
        raise ReleaseError("release manifest must be an object")
    required = {
        "schema_version",
        "project",
        "version",
        "release",
        "license",
        "archive_format",
        "archive_root_pattern",
        "output_directory",
        "source_equals_runtime",
        "checksums",
        "public_files",
        "source",
        "runtime",
        "excluded_paths",
        "verification",
        "support",
    }
    missing = sorted(required.difference(manifest))
    if missing:
        raise ReleaseError(f"manifest is missing required keys: {', '.join(missing)}")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ReleaseError(f"unsupported manifest schema: {manifest['schema_version']!r}")
    if manifest["project"] != "codex-adaptive-agents":
        raise ReleaseError("manifest project is not codex-adaptive-agents")
    if manifest["version"] != EXPECTED_VERSION:
        raise ReleaseError(f"manifest version must be {EXPECTED_VERSION}")
    try:
        version = _read_regular_file(VERSION_FILE).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError, ReleaseError) as exc:
        raise ReleaseError(f"cannot read VERSION: {exc}") from exc
    if version != EXPECTED_VERSION or manifest["version"] != version:
        raise ReleaseError("VERSION and release manifest disagree")
    if manifest["release"] != "python-source":
        raise ReleaseError("manifest release must be python-source")
    if manifest["license"] != "Apache-2.0":
        raise ReleaseError("manifest must declare Apache-2.0")
    if manifest["archive_format"] != "zip" or manifest["source_equals_runtime"] is not True:
        raise ReleaseError("manifest must describe a source-equals-runtime ZIP")
    if manifest.get("entrypoint") != "codex-adaptive-agents.py":
        raise ReleaseError("manifest entrypoint must be codex-adaptive-agents.py")
    if not isinstance(manifest["archive_root_pattern"], str):
        raise ReleaseError("archive_root_pattern must be a string")
    if "{version}" not in manifest["archive_root_pattern"] or "{target}" not in manifest["archive_root_pattern"]:
        raise ReleaseError("archive_root_pattern must contain {version} and {target}")
    source = manifest["source"]
    if not isinstance(source, dict):
        raise ReleaseError("manifest source must be an object")
    required_files = source.get("required_files")
    required_globs = source.get("required_globs")
    if not isinstance(required_files, list) or not required_files:
        raise ReleaseError("source.required_files must be a non-empty list")
    if not isinstance(required_globs, list) or not required_globs:
        raise ReleaseError("source.required_globs must be a non-empty list")
    for value in required_files:
        safe_relative(value)
    for value in required_globs:
        safe_pattern(value)
    public_files = manifest["public_files"]
    if not isinstance(public_files, list) or not public_files:
        raise ReleaseError("public_files must be a non-empty list")
    for value in public_files:
        safe_relative(value)
    excluded_paths = manifest["excluded_paths"]
    if not isinstance(excluded_paths, list):
        raise ReleaseError("excluded_paths must be a list")
    for value in excluded_paths:
        if not isinstance(value, str) or not value:
            raise ReleaseError(f"invalid excluded path: {value!r}")
    runtime = manifest["runtime"]
    if (not isinstance(runtime, dict) or runtime.get("same_as") != "source"
            or runtime.get("entrypoint") != "codex-adaptive-agents.py"):
        raise ReleaseError("runtime must explicitly equal source")
    checksums = manifest["checksums"]
    if not isinstance(checksums, dict) or checksums.get("package_file") != CHECKSUMS_NAME:
        raise ReleaseError("manifest checksum contract is incomplete")
    return manifest


def collect_source(manifest: dict[str, Any]) -> dict[str, SourceSnapshot]:
    source = manifest["source"]
    excluded_paths = manifest["excluded_paths"]
    collected: dict[str, SourceCandidate] = {}

    def add(relative: str, path: Path) -> None:
        relative = safe_relative(relative)
        if _excluded(relative, excluded_paths):
            raise ReleaseError(f"manifest required path is excluded: {relative}")
        if has_forbidden_component(relative):
            raise ReleaseError(f"manifest path contains forbidden component: {relative}")
        candidate = _source_candidate(path, relative)
        info = candidate.info
        parent_identity = candidate.parent_identity
        previous = collected.get(relative)
        if previous is not None and (
            previous.path != path
            or _file_identity(previous.info) != _file_identity(info)
            or previous.parent_identity != parent_identity
        ):
            raise ReleaseError(f"duplicate source path: {relative}")
        collected[relative] = candidate

    for raw in source["required_files"]:
        relative = safe_relative(raw)
        add(relative, PROJECT_ROOT / Path(*relative.split("/")))

    for raw_pattern in source["required_globs"]:
        pattern = safe_pattern(raw_pattern)
        matches = sorted(PROJECT_ROOT.glob(pattern), key=lambda item: item.as_posix())
        if not matches:
            raise ReleaseError(f"required glob matched nothing: {pattern}")
        for match in matches:
            try:
                relative = match.relative_to(PROJECT_ROOT).as_posix()
            except ValueError as exc:
                raise ReleaseError(f"glob escaped repository: {pattern}") from exc
            if match.is_symlink():
                raise ReleaseError(f"symlink is not allowed in glob: {relative}")
            if match.is_dir():
                for child in sorted(match.rglob("*"), key=lambda item: item.as_posix()):
                    child_relative = child.relative_to(PROJECT_ROOT).as_posix()
                    if child.is_symlink():
                        raise ReleaseError(f"symlink is not allowed in glob: {child_relative}")
                    if child.is_dir():
                        if has_forbidden_component(child_relative):
                            raise ReleaseError(f"forbidden directory in glob: {child_relative}")
                        continue
                    add(child_relative, child)
            else:
                add(relative, match)
    snapshots = {}
    for relative, candidate in sorted(collected.items()):
        snapshots[relative] = _snapshot_source(candidate, relative)
    return snapshots


def _copy_regular(source: SourceSnapshot, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as dst:
        dst.write(source.data)
    os.chmod(target, source.mode)


def copy_to_stage(files: dict[str, SourceSnapshot], stage_root: Path) -> None:
    for relative, source in files.items():
        target = stage_root / Path(*relative.split("/"))
        _copy_regular(source, target)


def regular_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ReleaseError(f"symlink found in package stage: {path}")
        if path.is_file():
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ReleaseError(f"non-regular package member: {path}")
            result.append(path)
        elif not path.is_dir():
            raise ReleaseError(f"non-regular package member: {path}")
    return result


def digest(path: Path) -> str:
    return hashlib.sha256(_read_regular_file(path)).hexdigest()


def relative_files(root: Path, *, include_checksums: bool = False) -> list[str]:
    names = []
    for path in regular_files(root):
        name = path.relative_to(root).as_posix()
        if include_checksums or name != CHECKSUMS_NAME:
            names.append(name)
    return names


def write_package_checksums(stage_root: Path) -> Path:
    checksum_path = stage_root / CHECKSUMS_NAME
    if checksum_path.exists() or checksum_path.is_symlink():
        raise ReleaseError("package stage already contains SHA256SUMS")
    lines = [
        f"{digest(stage_root / Path(*name.split('/')))}  {name}"
        for name in relative_files(stage_root)
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    _write_new_file_exclusively(stage_root, CHECKSUMS_NAME,
                                lambda handle: handle.write(payload))
    verify_package_checksums(stage_root)
    return checksum_path


def _parse_checksums(path: Path) -> dict[str, str]:
    try:
        lines = _read_regular_file(path).decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read checksum file {path}: {exc}") from exc
    if not lines:
        raise ReleaseError(f"empty checksum file: {path}")
    result: dict[str, str] = {}
    for line in lines:
        match = CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise ReleaseError(f"malformed checksum line in {path}: {line!r}")
        checksum, raw_name = match.groups()
        name = safe_relative(raw_name)
        if name == CHECKSUMS_NAME or name in result:
            raise ReleaseError(f"duplicate or self-referencing checksum entry: {name}")
        result[name] = checksum.lower()
    return result


def verify_package_checksums(stage_root: Path) -> None:
    checksum_path = stage_root / CHECKSUMS_NAME
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise ReleaseError("package SHA256SUMS is missing or not regular")
    entries = _parse_checksums(checksum_path)
    actual = set(relative_files(stage_root))
    if set(entries) != actual:
        missing = sorted(actual.difference(entries))
        extra = sorted(set(entries).difference(actual))
        raise ReleaseError(f"package checksum coverage mismatch; missing={missing}, extra={extra}")
    for name, expected in entries.items():
        actual_digest = digest(stage_root / Path(*name.split("/")))
        if actual_digest != expected:
            raise ReleaseError(f"package checksum mismatch: {name}")


def _public_document_files(root: Path) -> list[Path]:
    # Source and test code can contain checker fixtures or platform examples.
    # Public Markdown is the reader-facing surface whose links and paths must
    # be safe and resolvable.
    return sorted(root.rglob("*.md"), key=lambda item: item.as_posix())


def _check_markdown_links(path: Path, root: Path, text: str) -> None:
    for raw_target in MARKDOWN_LINK_RE.findall(text):
        target = raw_target.strip()
        if target.startswith("<"):
            end = target.find(">")
            if end < 0:
                raise ReleaseError(f"malformed Markdown link in {path}")
            target = target[1:end].strip()
        else:
            target = target.split(None, 1)[0]
        if not target or target.startswith(("#", "http://", "https://", "mailto:", "data:")):
            continue
        if target.startswith("file:") or target.startswith(("/", "\\")):
            raise ReleaseError(f"private or absolute Markdown link in {path}: {target}")
        if any(marker in target for marker in (".orchestrator-dev", ".reference", ".codex-adaptive-agents")):
            raise ReleaseError(f"private Markdown link in {path}: {target}")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        relative_target = unquote(parsed.path)
        if not relative_target:
            continue
        candidate = (path.parent / Path(*relative_target.split("/"))).resolve(strict=False)
        if not within(candidate, root) or not candidate.is_file() or candidate.is_symlink():
            raise ReleaseError(f"broken or escaping Markdown link in {path}: {target}")


def check_public_paths(root: Path) -> None:
    for path in _public_document_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReleaseError(f"public Markdown is not UTF-8: {path}: {exc}") from exc
        match = PRIVATE_PATH_RE.search(text)
        if match:
            raise ReleaseError(f"private machine path in public Markdown {path}: {match.group(0)}")
        _check_markdown_links(path, root, text)


def _zip_info(name: str, mode: int, *, directory: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    file_mode = stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | (stat.S_IMODE(mode) or 0o644)
    info.external_attr = file_mode << 16
    return info


def _write_new_file_exclusively(directory: Path, name: str, writer, *, mode: int = 0o644) -> Path:
    """Create and fill a new regular file without following a raced target link.

    POSIX uses an opened, no-follow directory descriptor for both exclusive
    creation and post-write identity checks. Windows has no standard-library
    dir_fd support; it uses O_EXCL plus parent-chain and handle identity checks.
    """
    safe_name = safe_relative(name)
    if "/" in safe_name:
        raise ReleaseError("exclusive output name must be one path component")
    directory = _absolute(directory)
    parent_identity = _checked_directory_chain(directory)
    directory_fd, opened_parent = _open_directory_chain(directory, parent_identity)
    if opened_parent != parent_identity:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ReleaseError(f"output directory changed during secure open: {directory}")
    target = directory / safe_name
    flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd: int | None = None
    created_info: os.stat_result | None = None
    created_inode: tuple[int, int] | None = None
    succeeded = False
    try:
        if directory_fd is not None:
            fd = os.open(safe_name, flags, mode, dir_fd=directory_fd)
        else:
            fd = os.open(target, flags, mode)
        initial_info = os.fstat(fd)
        if _is_link_or_reparse(initial_info) or not stat.S_ISREG(initial_info.st_mode):
            raise ReleaseError(f"exclusive output is not a regular file: {target}")
        created_info = initial_info
        created_inode = (int(initial_info.st_dev), int(initial_info.st_ino))
        os.fchmod(fd, mode) if hasattr(os, "fchmod") else None
        with os.fdopen(fd, "w+b") as handle:
            fd = None
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
            final_fd_info = os.fstat(handle.fileno())
            if _is_link_or_reparse(final_fd_info) or not stat.S_ISREG(final_fd_info.st_mode):
                raise ReleaseError(f"exclusive output changed type while writing: {target}")
            if (final_fd_info.st_dev, final_fd_info.st_ino) != (initial_info.st_dev, initial_info.st_ino):
                raise ReleaseError(f"exclusive output identity changed while writing: {target}")
            created_info = final_fd_info

        if directory_fd is not None:
            final_info = os.stat(safe_name, dir_fd=directory_fd, follow_symlinks=False)
        else:
            if _checked_directory_chain(directory) != parent_identity:
                raise ReleaseError(f"output directory changed while writing: {directory}")
            final_info = target.lstat()
        if (_is_link_or_reparse(final_info) or not stat.S_ISREG(final_info.st_mode)
                or created_info is None
                or not _path_handle_identity_matches(final_info, created_info)):
            raise ReleaseError(f"exclusive output path changed while writing: {target}")
        succeeded = True
        return target
    except FileExistsError as exc:
        raise ReleaseError(f"refusing to overwrite existing output: {target}") from exc
    except OSError as exc:
        raise ReleaseError(f"cannot create secure output {target}: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Remove only the inode this call created. On POSIX the dir_fd keeps
        # cleanup inside the checked directory even if its pathname changes.
        if created_inode is not None and not succeeded:
            try:
                if directory_fd is not None:
                    info = os.stat(safe_name, dir_fd=directory_fd, follow_symlinks=False)
                    if (int(info.st_dev), int(info.st_ino)) == created_inode:
                        os.unlink(safe_name, dir_fd=directory_fd)
                elif _checked_directory_chain(directory) == parent_identity:
                    info = target.lstat()
                    if (int(info.st_dev), int(info.st_ino)) == created_inode:
                        target.unlink()
            except (OSError, ReleaseError):
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def _prepare_output_directory(path: Path) -> Path:
    """Resolve the user-selected output once, then create it without links."""
    requested = _absolute(path)
    try:
        original = requested.lstat()
    except FileNotFoundError:
        original = None
    except OSError as exc:
        raise ReleaseError(f"cannot inspect output directory: {requested}") from exc
    if original is not None and (_is_link_or_reparse(original) or not stat.S_ISDIR(original.st_mode)):
        raise ReleaseError(f"refusing non-directory or linked output path: {requested}")
    resolved = requested.resolve(strict=False)
    try:
        current = resolved.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and (_is_link_or_reparse(current) or not stat.S_ISDIR(current.st_mode)):
        raise ReleaseError(f"refusing non-directory or linked output path: {resolved}")

    missing: list[str] = []
    ancestor = resolved
    while not ancestor.exists():
        if ancestor == Path(ancestor.anchor):
            raise ReleaseError(f"cannot find an existing output ancestor: {resolved}")
        missing.append(ancestor.name)
        ancestor = ancestor.parent
    ancestor = Path(os.path.abspath(os.fspath(ancestor)))
    expected = _checked_directory_chain(ancestor)
    directory_fd, opened = _open_directory_chain(ancestor, expected)
    if opened != expected:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ReleaseError(f"output ancestor changed during secure open: {ancestor}")
    if directory_fd is None:
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            _checked_directory_chain(resolved)
        except OSError as exc:
            raise ReleaseError(f"cannot create output directory: {resolved}: {exc}") from exc
        return resolved

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    expected_full = list(expected)
    try:
        for component in reversed(missing):
            try:
                os.mkdir(component, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            expected_full.append(_directory_identity(os.fstat(directory_fd)))
        if _checked_directory_chain(resolved) != tuple(expected_full):
            raise ReleaseError(f"output directory changed during creation: {resolved}")
        info = os.fstat(directory_fd)
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"unsafe output directory: {resolved}")
    except OSError as exc:
        raise ReleaseError(f"cannot create secure output directory: {resolved}: {exc}") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return resolved


def archive_root(manifest: dict[str, Any]) -> str:
    root = manifest["archive_root_pattern"].format(version=manifest["version"], target="python")
    return safe_relative(root)


def create_archive(stage_root: Path, archive_path: Path, manifest: dict[str, Any]) -> None:
    root = archive_root(manifest)
    files = relative_files(stage_root, include_checksums=True)
    def write_archive(handle) -> None:
        with zipfile.ZipFile(
            handle,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as output:
            output.writestr(_zip_info(root + "/", 0o755, directory=True), b"")
            for relative in files:
                source = stage_root / Path(*relative.split("/"))
                info = source.lstat()
                if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                    raise ReleaseError(f"unsafe staged package member: {relative}")
                output.writestr(
                    _zip_info(root + "/" + relative, info.st_mode),
                    _read_regular_file(source, expected=info),
                )

    _write_new_file_exclusively(archive_path.parent, archive_path.name, write_archive)


def _member_relative(name: str, root: str) -> str | None:
    if name == root + "/":
        return None
    prefix = root + "/"
    if not name.startswith(prefix):
        raise ReleaseError(f"archive member is outside package root: {name!r}")
    relative = name[len(prefix) :]
    if relative.endswith("/"):
        relative = relative[:-1]
    return safe_relative(relative)


def validate_archive(archive_path: Path, manifest: dict[str, Any]) -> None:
    """Validate against a fresh manifest-derived source snapshot."""
    _validate_archive(archive_path, manifest, collect_source(manifest))


def _validate_archive(
    archive_path: Path,
    manifest: dict[str, Any],
    source_snapshot: dict[str, SourceSnapshot],
) -> None:
    expected_files = set(source_snapshot) | {CHECKSUMS_NAME}
    expected_directories = {
        "/".join(parts[:index])
        for relative in expected_files
        for parts in [relative.split("/")]
        for index in range(1, len(parts))
    }
    root = archive_root(manifest)
    archive_bytes = _read_regular_file(archive_path)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".verify-python-release-", dir=temp_root) as temporary:
        extracted = Path(temporary) / root
        seen_files: set[str] = set()
        seen_directories: set[str] = set()
        root_seen = False
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
                for info in archive.infolist():
                    if "\x00" in info.filename or "\\" in info.filename:
                        raise ReleaseError(f"unsafe archive member: {info.filename!r}")
                    mode = (info.external_attr >> 16) & 0xFFFF
                    type_bits = stat.S_IFMT(mode)
                    if type_bits not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ReleaseError(f"archive contains a link or special file: {info.filename!r}")
                    if info.filename == root + "/":
                        if not info.is_dir() or type_bits not in (0, stat.S_IFDIR) or root_seen:
                            raise ReleaseError("archive root directory is duplicated or malformed")
                        root_seen = True
                        continue
                    relative = _member_relative(info.filename, root)
                    if relative is None:
                        raise ReleaseError("invalid archive root member")
                    if relative in seen_files or relative in seen_directories:
                        raise ReleaseError(f"duplicate archive member: {relative}")
                    if info.is_dir():
                        if type_bits not in (0, stat.S_IFDIR):
                            raise ReleaseError(f"archive directory has file type bits: {relative!r}")
                        if relative not in expected_directories:
                            raise ReleaseError(f"archive contains an unneeded directory: {relative!r}")
                        seen_directories.add(relative)
                        continue
                    if type_bits not in (0, stat.S_IFREG):
                        raise ReleaseError(f"archive file has invalid type bits: {relative!r}")
                    seen_files.add(relative)
                if not root_seen:
                    raise ReleaseError("archive root directory is missing")
                if seen_files != expected_files:
                    missing = sorted(expected_files.difference(seen_files))
                    extra = sorted(seen_files.difference(expected_files))
                    raise ReleaseError(f"archive payload differs from source; missing={missing}, extra={extra}")
                archive.extractall(Path(temporary))
        except (zipfile.BadZipFile, EOFError, RuntimeError, zlib.error) as exc:
            raise ReleaseError(f"invalid or unreadable release archive: {exc}") from exc
        if not extracted.is_dir():
            raise ReleaseError("archive extracted without its root directory")
        verify_package_checksums(extracted)
        for relative, expected in source_snapshot.items():
            actual = digest(extracted / Path(*relative.split("/")))
            if actual != expected.sha256:
                raise ReleaseError(f"archive payload differs from current source: {relative}")
        check_public_paths(extracted)


def build_package(output_dir: Path, manifest: dict[str, Any]) -> Path:
    files = collect_source(manifest)
    output_dir = _prepare_output_directory(output_dir)
    root = archive_root(manifest)
    archive_path = output_dir / f"{root}.zip"
    if archive_path.exists() or archive_path.is_symlink():
        raise ReleaseError(f"refusing to overwrite existing archive: {archive_path}")
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".stage-python-release-", dir=temp_root) as temporary:
        stage = Path(temporary) / root
        stage.mkdir()
        copy_to_stage(files, stage)
        check_public_paths(stage)
        write_package_checksums(stage)
        create_archive(stage, archive_path, manifest)
    _validate_archive(archive_path, manifest, files)
    return archive_path


def write_external_checksums(output_dir: Path, archives: list[Path]) -> Path:
    output_dir = _prepare_output_directory(output_dir)
    checksum_path = output_dir / CHECKSUMS_NAME
    if checksum_path.exists() or checksum_path.is_symlink():
        raise ReleaseError(f"refusing to overwrite external checksum file: {checksum_path}")
    if not archives:
        raise ReleaseError("no release archives were produced")
    normalized_archives = [_absolute(path) for path in archives]
    names = [safe_relative(path.name) for path in normalized_archives]
    if len(set(names)) != len(names):
        raise ReleaseError("duplicate archive names in external checksum set")
    snapshots: dict[str, bytes] = {}
    for archive in normalized_archives:
        if archive.parent.resolve(strict=True) != output_dir.resolve(strict=True):
            raise ReleaseError(f"external checksum archive is outside output directory: {archive}")
        snapshots[archive.name] = _read_regular_file(archive)
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in snapshots.items()}
    lines = [f"{expected[name]}  {name}" for name in sorted(expected)]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    _write_new_file_exclusively(output_dir, CHECKSUMS_NAME,
                                lambda handle: handle.write(payload))
    actual: dict[str, str] = {}
    written = _read_regular_file(checksum_path)
    if written != payload:
        raise ReleaseError("external checksum file changed after secure creation")
    for line in written.decode("utf-8").splitlines():
        match = CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise ReleaseError("malformed external checksum line")
        checksum, name = match.groups()
        safe_relative(name)
        if name in actual:
            raise ReleaseError(f"duplicate external checksum entry: {name}")
        actual[name] = checksum.lower()
    if actual != expected:
        raise ReleaseError("external checksum coverage mismatch")
    return checksum_path


def build_release(output_dir: Path) -> tuple[Path, Path]:
    manifest = load_manifest()
    output_dir = _prepare_output_directory(output_dir.expanduser())
    expected_archive = output_dir / f"{archive_root(manifest)}.zip"
    expected_checksums = output_dir / CHECKSUMS_NAME
    if expected_archive.exists() or expected_archive.is_symlink():
        raise ReleaseError(f"refusing to overwrite existing archive: {expected_archive}")
    if expected_checksums.exists() or expected_checksums.is_symlink():
        raise ReleaseError(f"refusing to overwrite external checksum file: {expected_checksums}")
    archive_path = build_package(output_dir, manifest)
    checksum_path = write_external_checksums(output_dir, [archive_path])
    return archive_path, checksum_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "dist" / "0.9.0-python",
        help="directory for the new ZIP and external SHA256SUMS",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        archive_path, checksum_path = build_release(args.output_dir)
    except (ReleaseError, OSError, ValueError) as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "archive": str(archive_path),
        "external_checksums": str(checksum_path),
        "version": EXPECTED_VERSION,
        "source_equals_runtime": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
