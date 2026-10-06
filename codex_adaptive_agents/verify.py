"""Trusted public-protocol verifier for the Python native smoke.

This module intentionally has no import-time process, network, configuration,
or filesystem side effects.  ``check`` is also usable by the hidden
``internal-smoke-check`` command: its oracle runs from this installed module,
outside the candidate project, through bounded ``sys.executable -I -B`` child
processes.  ``run`` retains only a small, normalized public lifecycle event
stream; it never persists protocol messages, command text, reasoning, session
metadata, or authentication data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any, Iterable, Mapping


MAX_RUNTIME_SECONDS = 600
CHECK_TIMEOUT_SECONDS = 90
SCRIPT_TIMEOUT_SECONDS = 5
MAX_PUBLIC_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 12000
DEFAULT_EXPECTED_ROLE = "adaptive_luna_xhigh"
ALLOWED_ROLES = frozenset(
    f"adaptive_luna_{effort}" for effort in ("low", "medium", "high", "xhigh", "max")
)
ROLE_RE = re.compile(r"^adaptive_luna_(?:low|medium|high|xhigh|max)$")
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class VerificationError(RuntimeError):
    """A bounded, verifier-authored failure without provider output."""


class ScriptError(VerificationError):
    """A fixture process failed for a known, safe reason."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(
            {
                "exit": "smoke script failed",
                "start": "smoke script could not start",
                "timeout": "smoke script timed out",
                "output": "smoke script output exceeded limit",
                "protocol": "smoke script output was invalid",
            }.get(kind, "smoke script failed")
        )


_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_AGENTS_SEED = (
    b"# Native smoke workspace\n\n"
    b"This file is verifier-owned and immutable during the smoke.\n"
)


def _asset(name: str) -> bytes:
    """Read one bundled fixture only when an operation actually needs it."""

    if name not in {"contract.md", "normalize_tags.py", "unique_numbers.py"}:
        raise VerificationError("unknown embedded fixture")
    path = _ASSET_DIR / name
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise VerificationError("embedded fixture is unsafe")
        if info.st_size > MAX_PUBLIC_MESSAGE_BYTES:
            raise VerificationError("embedded fixture exceeds limit")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise VerificationError("embedded fixture is missing") from exc


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    data = _read_file(path)
    if data is None:
        raise VerificationError("required file is missing")
    return _sha_bytes(data)


def _read_file(path: Path) -> bytes | None:
    """Read a bounded regular file without following a final link."""

    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError("unsafe file path")
    if info.st_size > 64 * 1024 * 1024:
        raise VerificationError("file exceeds read limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise VerificationError("file could not be read") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(64 * 1024 * 1024 + 1)
    except OSError as exc:
        raise VerificationError("file could not be read") from exc
    if len(data) > 64 * 1024 * 1024:
        raise VerificationError("file exceeds read limit")
    return data


def _reject_link_components(path: Path) -> None:
    """Reject links/reparse points in all existing components of ``path``."""

    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise VerificationError("symlink or reparse point rejected")


def _absolute_path(value: Path | str, *, allow_missing: bool = False) -> Path:
    if value is None:
        raise VerificationError("path is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    if "\x00" in str(path) or ".." in path.parts:
        raise VerificationError("absolute clean path required")
    _reject_link_components(path)
    if not allow_missing and not path.exists():
        raise VerificationError("path is missing")
    return path


def _contained(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for fixture/official processes with provider overrides gone."""

    try:
        from . import platform as runtime_platform

        env = dict(runtime_platform.clean_env())
    except (ImportError, AttributeError):
        env = dict(os.environ)
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "CODEX_HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": "/usr/bin:/bin" if os.name != "nt" else env.get("PATH", ""),
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
    )
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _json_load(raw: bytes | str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(_value: str) -> Any:
        raise ValueError("non-finite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _safe_error(error: BaseException) -> str:
    if isinstance(error, VerificationError):
        return str(error)[:240]
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "command timed out"
    if isinstance(error, FileNotFoundError):
        return "required command or file is missing"
    if isinstance(error, OSError):
        return "operating system error"
    return error.__class__.__name__[:80]


def _bounded_run(argv, *, cwd, env, input_data, timeout, limit=MAX_OUTPUT_BYTES):
    from . import platform
    try:
        return platform.run([str(item) for item in argv], cwd=cwd, env=dict(env),
                            input=input_data, timeout=max(0.1, timeout), limit=limit)
    except RuntimeError as error:
        message = str(error).lower()
        kind = 'timeout' if 'timed out' in message else ('output' if 'output' in message else 'start')
        raise ScriptError(kind) from error


def _split_output(raw: bytes) -> list[bytes]:
    """Split strict line output; empty bytes means no lines."""

    if raw == b"":
        return []
    if not raw.endswith(b"\n"):
        return [b"\x00missing-final-newline"]
    lines = raw[:-1].split(b"\n")
    # Text streams on Windows conventionally emit CRLF.  Treat a CR directly
    # before LF as the line terminator while preserving any CR inside content.
    return [line[:-1] if line.endswith(b"\r") else line for line in lines]


def _same_lines(actual: list[bytes], expected: list[bytes]) -> bool:
    return actual == expected


def _trim_ascii(value: bytes) -> bytes:
    start, end = 0, len(value)
    while start < end and value[start] in (0x20, 0x09, 0x0D):
        start += 1
    while end > start and value[end - 1] in (0x20, 0x09, 0x0D):
        end -= 1
    value = value[start:end]
    return bytes(ch + 32 if 65 <= ch <= 90 else ch for ch in value)


def _tag_expected(input_data: str) -> list[bytes]:
    seen: set[bytes] = set()
    result: list[bytes] = []
    for line in input_data.encode("utf-8").split(b"\n"):
        value = _trim_ascii(line)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_number(value: bytes) -> bytes | None:
    if not value:
        return None
    start = 1 if value[:1] in (b"+", b"-") else 0
    if start == len(value) or any(ch < 48 or ch > 57 for ch in value[start:]):
        return None
    try:
        number = int(value.decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError, OverflowError):
        return None
    if number < -10000 or number > 10000:
        return None
    return str(number).encode("ascii")


def _number_expected(input_data: str) -> tuple[list[bytes], bool]:
    if input_data == "":
        return [], True
    body = input_data[:-1] if input_data.endswith("\n") else input_data
    lines = body.split("\n")
    seen: set[bytes] = set()
    result: list[bytes] = []
    for line in lines:
        value = _parse_number(line.encode("utf-8"))
        if value is None:
            return [], False
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result, True


def _next_random(seed: int) -> tuple[int, int]:
    seed = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
    return seed, seed >> 16


def _tag_cases() -> list[tuple[str, list[bytes]]]:
    cases = [
        "",
        "  Alpha\t\r\nBETA\r\nalpha\n\t \r\nA B\nA\tB\n",
        "Z\nz\nÉclair\néclair\n A-Z \n",
        "  No-final-newline\nSECOND",
    ]
    seed = 0x9E3779B97F4A7C15
    for _index in range(128):
        seed, value = _next_random(seed)
        rows: list[str] = []
        count = 4 + value % 5
        for index in range(count):
            seed, word_value = _next_random(seed)
            row = f"Tag{word_value % 20:02d}"
            if index % 2 == 0:
                row = " \t" + row
            if index % 3 == 0:
                row += "\r "
            rows.append(row)
        cases.append("\n".join(rows) + "\n")
    return [(value, _tag_expected(value)) for value in cases]


def _number_cases() -> list[tuple[str, list[bytes]]]:
    cases = [
        "",
        "-10000\n+10000\n0000\n-0000\n+00001\n-00001\n",
        "1\n01\n+1\n-1\n-01\n10000\n-10000\n",
        "-10000\n10000\n0001\n+1",
    ]
    seed = 0x243F6A8885A308D3
    for _index in range(128):
        seed, value = _next_random(seed)
        rows: list[str] = []
        count = 6 + value % 5
        for index in range(count):
            seed, random_value = _next_random(seed)
            number = random_value % 10001
            if index % 4 in (1, 3):
                row = "-" + "000" + str(number)
            elif index % 4 == 0:
                row = "+" + "00" + str(number)
            else:
                row = "00" + str(number)
            rows.append(row)
        cases.append("\n".join(rows) + "\n")
    return [(value, _number_expected(value)[0]) for value in cases]


def _invalid_number_cases() -> list[str]:
    cases = [
        "0\n+1\n-10000\n10000\nbad\n",
        "10001\n",
        "-10001\n",
        "1 \n",
        "１\n",
    ]
    # The five historical malformed cases are kept byte-for-byte compatible
    # with the Go verifier.  Empty input and a valid line without a terminal
    # newline remain in the positive golden corpus.
    return cases


def _scope_files(project: Path) -> tuple[list[str], list[str]]:
    """Return allowed regular files and unsafe/unexpected relative paths."""

    allowed = {"AGENTS.md", "contract.md", "normalize_tags.py", "unique_numbers.py"}
    files: list[str] = []
    errors: list[str] = []
    try:
        root_info = project.lstat()
    except FileNotFoundError:
        return [], ["project directory is missing"]
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return [], ["project must be a regular directory"]
    try:
        entries = list(os.scandir(project))
    except OSError:
        return [], ["project could not be read"]
    for entry in entries:
        rel = entry.name
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            errors.append("unsafe project path: " + rel)
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            errors.append("unsafe project path: " + rel)
        elif rel == ".codex-adaptive-agents" and stat.S_ISDIR(info.st_mode):
            # The client may maintain project-local state here.  It is never
            # read by this verifier and is the only ignored subtree.
            continue
        elif stat.S_ISDIR(info.st_mode):
            errors.append("unexpected project directory: " + rel)
        elif stat.S_ISREG(info.st_mode) and rel in allowed:
            files.append(rel)
        else:
            errors.append("unexpected project file: " + rel)
    for name in sorted(allowed - set(files)):
        errors.append("missing project file: " + name)
    return sorted(files), sorted(errors)


def _script_command(path: Path) -> list[str]:
    # -I prevents project/environment module injection; -B prevents pycache.
    return [sys.executable, "-I", "-B", str(path)]


def _run_script(project: Path, name: str, input_text: str, timeout: float = SCRIPT_TIMEOUT_SECONDS) -> bytes:
    path = project / name
    try:
        result = _bounded_run(
            _script_command(path),
            cwd=project,
            env=_minimal_env(),
            input_data=input_text.encode("utf-8"),
            timeout=timeout,
            limit=MAX_OUTPUT_BYTES,
        )
    except ScriptError:
        raise
    if result.returncode < 0 or result.returncode >= 0xC0000000:
        raise ScriptError("signal")
    if result.returncode != 0:
        raise ScriptError("exit")
    return bytes(result.stdout or b"")



def _run_fixture_suite(project: Path) -> dict[str, Any]:
    """Run the trusted fixture oracle from this installed verifier.

    This code is outside the candidate project during the sandbox command.  A
    separate process is used for every case, with ``-I -B`` and bounded
    platform execution, so a fixture cannot import or mutate the verifier.
    """

    started = time.monotonic()
    counts = {"normalize_cases": 0, "number_cases": 0, "invalid_number_cases": 0}
    for input_text, expected in _tag_cases():
        if time.monotonic() - started >= CHECK_TIMEOUT_SECONDS:
            raise VerificationError("trusted fixture check timed out")
        output = _run_script(project, "normalize_tags.py", input_text,
                             min(SCRIPT_TIMEOUT_SECONDS, CHECK_TIMEOUT_SECONDS - (time.monotonic() - started)))
        if not _same_lines(_split_output(output), expected):
            raise VerificationError("normalize_tags.py golden output mismatch")
        counts["normalize_cases"] += 1
    for input_text, expected in _number_cases():
        if time.monotonic() - started >= CHECK_TIMEOUT_SECONDS:
            raise VerificationError("trusted fixture check timed out")
        output = _run_script(project, "unique_numbers.py", input_text,
                             min(SCRIPT_TIMEOUT_SECONDS, CHECK_TIMEOUT_SECONDS - (time.monotonic() - started)))
        if not _same_lines(_split_output(output), expected):
            raise VerificationError("unique_numbers.py golden output mismatch")
        counts["number_cases"] += 1
    for input_text in _invalid_number_cases():
        if time.monotonic() - started >= CHECK_TIMEOUT_SECONDS:
            raise VerificationError("trusted fixture check timed out")
        try:
            _run_script(project, "unique_numbers.py", input_text,
                        min(SCRIPT_TIMEOUT_SECONDS, CHECK_TIMEOUT_SECONDS - (time.monotonic() - started)))
        except ScriptError as error:
            # Only an ordinary nonzero fixture exit is evidence of rejection.
            # Start, timeout, output-limit and protocol errors are verifier
            # failures and must never be counted as an invalid-input pass.
            if error.kind != "exit":
                raise VerificationError("invalid fixture did not complete normally") from error
        else:
            raise VerificationError("unique_numbers.py accepted invalid input")
        counts["invalid_number_cases"] += 1
    return counts


def check(project: Path) -> dict[str, Any]:
    """Run the trusted 132+132 valid and invalid Python fixture oracle."""

    result: dict[str, Any] = {
        "ok": False,
        "contract": "embedded-v1",
        "network": False,
        "trusted_validator": "installed codex_adaptive_agents.verify",
    }
    try:
        root = _absolute_path(project)
        _reject_link_components(root)
        files, errors = _scope_files(root)
        if errors:
            raise VerificationError(errors[0])
        expected_files = ["AGENTS.md", "contract.md", "normalize_tags.py", "unique_numbers.py"]
        if files != expected_files:
            raise VerificationError("smoke fixture scope mismatch")
        contract = _read_file(root / "contract.md")
        if contract != _asset("contract.md"):
            raise VerificationError("contract.md changed from embedded fixture")
        for name in ("normalize_tags.py", "unique_numbers.py"):
            if _read_file(root / name) is None:
                raise VerificationError(name + " is missing or unsafe")
        counts = _run_fixture_suite(root)
        result.update(counts)
        if counts["normalize_cases"] < 132 or counts["number_cases"] < 132:
            raise VerificationError("golden corpus is incomplete")
        if counts["invalid_number_cases"] != 5:
            raise VerificationError("invalid corpus is incomplete")
        result["files"] = expected_files
        result["invalid_number_rejected"] = True
        result["ok"] = True
    except Exception as exc:
        result["error"] = _safe_error(exc)
    return result

# ---------------------------------------------------------------------------
# Public lifecycle evidence


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _bool(value: Any) -> bool:
    return type(value) is bool


def _finite_number(value: Any) -> float | None:
    # bool is an int subclass, but it is never a valid elapsed/depth value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _map(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _event_params(event: Mapping[str, Any]) -> Mapping[str, Any]:
    params = event.get("params")
    return params if isinstance(params, Mapping) else event


def _event_name(event: Mapping[str, Any]) -> str:
    name = _string(event.get("event")) or _string(event.get("method"))
    return name.replace(".", "/")


def _event_thread_id(event: Mapping[str, Any]) -> str:
    params = _event_params(event)
    value = _first(event, "threadId", "thread_id")
    if value is None:
        value = _first(params, "threadId", "thread_id")
    return _string(value)


def _event_elapsed(event: Mapping[str, Any]) -> float | None:
    value = _first(event, "elapsed_seconds", "elapsedSeconds")
    if value is None:
        params = _event_params(event)
        value = _first(params, "elapsed_seconds", "elapsedSeconds")
    if value is None:
        return None
    return _finite_number(value)


def _status_type(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("type")
    return value if isinstance(value, str) else None


def _safe_status(value: Any) -> dict[str, str | None]:
    return {"type": _status_type(value)}


def _safe_effective(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result, _errors = _normalize_effective(value, "public effective metadata")
    return result


_EFFECTIVE_KEYS = ("model", "modelProvider", "provider", "reasoningEffort", "reasoning_effort")
_PUBLIC_METADATA_INVALID = "_public_metadata_invalid"


def _normalize_effective(value: Any, label: str) -> tuple[dict[str, Any], list[str]]:
    """Project known public fields and retain safe shape/conflict diagnostics."""
    if value is None:
        return {}, []
    if not isinstance(value, Mapping):
        return {}, [label + " has an unsupported public shape"]
    result: dict[str, Any] = {}
    errors: list[str] = []
    for key in _EFFECTIVE_KEYS:
        if key not in value:
            continue
        item = value[key]
        if item is None:
            result[key] = None
        elif isinstance(item, str) and item.strip():
            result[key] = item
        else:
            if label == "root client effective metadata" and key == "model":
                errors.append("root client model has an unsupported public shape")
            elif label == "root client effective metadata" and key in ("reasoningEffort", "reasoning_effort"):
                errors.append("root client effort has an unsupported public shape")
            else:
                errors.append(label + " has an unsupported public shape")
    for aliases, description in ((('modelProvider', 'provider'), 'provider'),
                                 (('reasoningEffort', 'reasoning_effort'), 'effort')):
        claims = [result[key] for key in aliases if key in result and result[key] is not None]
        if len(set(claims)) > 1:
            errors.append(label + " " + description + " aliases conflict")
    return result, errors


def _spawn_claims(thread: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool, bool]:
    """Return a safe spawn claim, whether one was present, and shape conflict."""
    candidates: list[Mapping[str, Any]] = []
    invalid = False
    for value in (thread.get("spawn"),):
        if value is None:
            continue
        if isinstance(value, Mapping):
            candidates.append(value)
        else:
            invalid = True
    source = thread.get("source")
    if source is not None and not isinstance(source, Mapping):
        invalid = True
    if isinstance(source, Mapping):
        aliases: list[Mapping[str, Any]] = []
        for alias in ("subagent", "subAgent"):
            if alias not in source or source[alias] is None:
                continue
            subagent = source[alias]
            if not isinstance(subagent, Mapping):
                invalid = True
                continue
            birth = subagent.get("thread_spawn")
            if birth is None:
                continue
            if isinstance(birth, Mapping):
                aliases.append(birth)
            else:
                invalid = True
        candidates.extend(aliases)
    if not candidates:
        return None, False, invalid
    result: dict[str, Any] = {}
    for key in ("parent_thread_id", "agent_role", "depth"):
        values: list[Any] = []
        for candidate in candidates:
            if key not in candidate:
                continue
            item = candidate[key]
            if item is None:
                continue
            if key == "depth":
                if _finite_number(item) is None:
                    invalid = True
                    continue
            elif not isinstance(item, str) or not item.strip():
                invalid = True
                continue
            values.append(item)
        if values:
            if any(value != values[0] for value in values[1:]):
                invalid = True
            result[key] = values[0]
    return result, True, invalid


def normalize_public_thread_metadata(thread: Any) -> dict[str, Any]:
    """Safely retain verifier-relevant fields and a fixed invalid-shape marker.

    The marker carries no source values.  It prevents transport normalization
    from turning a malformed known field into an apparently missing field.
    """
    if not isinstance(thread, Mapping):
        return {_PUBLIC_METADATA_INVALID: True}
    result: dict[str, Any] = {}
    # Transport may already have projected this event.  Preserve only the
    # boolean taint bit, never a value that caused it.
    invalid = _bool(thread.get(_PUBLIC_METADATA_INVALID))
    for key in ("id", "parentThreadId", "agentRole"):
        if key not in thread:
            continue
        item = thread[key]
        if (item is None or isinstance(item, str) and
                (item.strip() or key == "parentThreadId")):
            result[key] = item
        else:
            invalid = True
    status = thread.get("status")
    if status is not None and not isinstance(status, (Mapping, str)):
        invalid = True
    result["status"] = _safe_status(status)
    spawn, spawn_present, spawn_invalid = _spawn_claims(thread)
    invalid = invalid or spawn_invalid
    if spawn_present and spawn is not None:
        result["spawn"] = spawn

    top, top_errors = _normalize_effective(thread, "child effective metadata")
    invalid = invalid or bool(top_errors)
    for key, value in top.items():
        result[key] = value
    for name in ("effective", "requested"):
        if name not in thread or thread[name] is None:
            continue
        values, field_errors = _normalize_effective(thread[name], "child " + name + " metadata")
        invalid = invalid or bool(field_errors)
        if values:
            result[name] = values
    if invalid:
        result[_PUBLIC_METADATA_INVALID] = True
    return result


def _safe_thread_metadata(thread: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_public_thread_metadata(thread)


def _thread_from_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = event.get("thread")
    if isinstance(value, Mapping):
        return value
    params = _event_params(event)
    value = params.get("thread")
    return value if isinstance(value, Mapping) else None


def _turn_from_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = event.get("turn")
    if isinstance(value, Mapping):
        return value
    params = _event_params(event)
    value = params.get("turn")
    return value if isinstance(value, Mapping) else None


def _item_from_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = event.get("item")
    if isinstance(value, Mapping):
        return value
    params = _event_params(event)
    value = params.get("item")
    return value if isinstance(value, Mapping) else None


def _turn_status(event: Mapping[str, Any]) -> str | None:
    turn = _turn_from_event(event)
    if turn is not None and "status" in turn:
        return _status_type(turn.get("status"))
    params = _event_params(event)
    return _status_type(_first(event, "status") if "status" in event else params.get("status"))


def _turn_id(event: Mapping[str, Any]) -> str:
    turn = _turn_from_event(event)
    return _string(turn.get("id")) if turn is not None else ""


@dataclass
class _PublicDerived:
    root_id: str = ""
    root_effective: dict[str, Any] = field(default_factory=dict)
    root_effectives: list[dict[str, Any]] = field(default_factory=list)
    root_requested: dict[str, Any] = field(default_factory=dict)
    root_request_count: int = 0
    child_ids: set[str] = field(default_factory=set)
    child_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    thread_started_metadata: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metadata_count: dict[str, int] = field(default_factory=dict)
    read_results: set[str] = field(default_factory=set)
    read_requests: list[dict[str, Any]] = field(default_factory=list)
    thread_statuses: dict[str, list[str | None]] = field(default_factory=dict)
    thread_status_events: dict[str, list[tuple[str | None, float | None, int]]] = field(default_factory=dict)
    turn_statuses: dict[str, list[str | None]] = field(default_factory=dict)
    turn_starts: dict[str, list[float | None]] = field(default_factory=dict)
    turn_ends: dict[str, list[float | None]] = field(default_factory=dict)
    turn_start_ids: dict[str, list[str]] = field(default_factory=dict)
    turn_end_ids: dict[str, list[str]] = field(default_factory=dict)
    turn_end_events: dict[str, list[str]] = field(default_factory=dict)
    turn_start_orders: dict[str, list[int]] = field(default_factory=dict)
    turn_end_orders: dict[str, list[int]] = field(default_factory=dict)
    turn_end_order: dict[str, int] = field(default_factory=dict)
    metadata_order: dict[str, int] = field(default_factory=dict)
    spawn_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    selector_invoked: bool = False
    tests_failed: bool = False


def _append(mapping: dict[str, list[Any]], key: str, value: Any) -> None:
    mapping.setdefault(key, []).append(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _derive_public(events: Any) -> _PublicDerived:
    derived = _PublicDerived()
    if not isinstance(events, list):
        derived.errors.append("public events are not a list")
        return derived

    last_elapsed: float | None = None
    for index, event_value in enumerate(events):
        if not isinstance(event_value, Mapping):
            derived.errors.append("public event is not an object")
            continue
        event = event_value
        name = _event_name(event)
        if not name:
            derived.errors.append("public event is missing method")
            continue
        params = _event_params(event)
        thread_id = _event_thread_id(event)
        elapsed_present = "elapsed_seconds" in event or "elapsedSeconds" in event or (
            params is not event and ("elapsed_seconds" in params or "elapsedSeconds" in params)
        )
        elapsed = _event_elapsed(event)
        if elapsed_present and elapsed is None:
            derived.errors.append("public elapsed timestamp is not finite")
        elif elapsed is not None and elapsed < 0:
            derived.errors.append("public elapsed timestamp is negative")
        elif elapsed is not None:
            if last_elapsed is not None and elapsed < last_elapsed:
                derived.errors.append("public elapsed timestamps are not globally nondecreasing")
            last_elapsed = elapsed

        # This flag is an out-of-band public result marker.  Check it before
        # the event-specific branches below because those branches intentionally
        # continue after extracting their safe fields.
        if _bool(event.get("tests_failed")):
            derived.tests_failed = True

        # A client-side synthetic event has no id.  Any public server request
        # (especially an approval request) is outside this verifier's grant.
        if "approval" in name.lower() or "approval" in _string(event.get("method")).lower():
            derived.errors.append("unexpected approval request")
        if "id" in event and "method" in event and name != "public-request":
            derived.errors.append("unexpected server request")

        if name == "root/client-effective":
            if thread_id:
                derived.root_id = thread_id
            effective_value = event.get("metadata")
            if not isinstance(effective_value, Mapping):
                effective_value = event.get("effective")
            if not isinstance(effective_value, Mapping):
                effective_value = event
            model_value = effective_value.get("model")
            if "model" in effective_value and model_value is not None and (
                not isinstance(model_value, str) or not model_value.strip()
            ):
                derived.errors.append("root client model has an unsupported public shape")
            effective, effective_errors = _normalize_effective(effective_value, "root client effective metadata")
            derived.errors.extend(effective_errors)
            if derived.root_effectives:
                if effective != derived.root_effectives[-1]:
                    derived.errors.append("conflicting root client effective metadata")
                else:
                    derived.errors.append("duplicate root client effective metadata")
            derived.root_effectives.append(effective)
            derived.root_effective = effective
            continue

        if name == "root/client-requested":
            if thread_id:
                derived.root_id = derived.root_id or thread_id
            requested_value = event.get("metadata")
            if not isinstance(requested_value, Mapping):
                derived.errors.append("root client requested metadata has an unsupported public shape")
            requested, request_errors = _normalize_effective(requested_value, "root client requested metadata")
            derived.errors.extend(request_errors)
            if isinstance(requested_value, Mapping) and "allowProviderModelFallback" in requested_value:
                fallback = requested_value["allowProviderModelFallback"]
                if fallback is not False:
                    derived.errors.append("root client provider fallback was not explicitly disabled")
                requested["allowProviderModelFallback"] = fallback if type(fallback) is bool else None
            derived.root_request_count += 1
            if derived.root_request_count > 1:
                derived.errors.append("duplicate root client requested metadata")
            derived.root_requested = requested
            continue

        if name == "thread/started":
            thread = _thread_from_event(event)
            if thread is None:
                derived.errors.append("thread/started missing thread")
                continue
            meta = _safe_thread_metadata(thread)
            identifier = _string(meta.get("id"))
            if _bool(meta.get(_PUBLIC_METADATA_INVALID)):
                derived.errors.append("public thread metadata has malformed known fields or conflicting aliases")
            if not identifier:
                derived.errors.append("thread/started missing id")
            elif not derived.root_id and not _string(meta.get("parentThreadId")):
                # This fallback is only used when a root/client-effective
                # event is unavailable; a later root id still removes it.
                derived.root_id = identifier
            else:
                derived.child_ids.add(identifier)
            if identifier:
                derived.thread_started_metadata.setdefault(identifier, []).append(meta)
            continue

        if name == "child/public-metadata":
            thread = _thread_from_event(event)
            if thread is None:
                derived.errors.append("child metadata missing thread")
                continue
            meta = _safe_thread_metadata(thread)
            identifier = _string(meta.get("id"))
            if _bool(meta.get(_PUBLIC_METADATA_INVALID)):
                derived.errors.append("public thread metadata has malformed known fields or conflicting aliases")
            if not identifier:
                derived.errors.append("child metadata missing id")
                continue
            derived.child_ids.add(identifier)
            derived.child_metadata[identifier] = meta
            derived.metadata_count[identifier] = derived.metadata_count.get(identifier, 0) + 1
            derived.read_results.add(identifier)
            derived.metadata_order.setdefault(identifier, index)
            continue

        if name == "public-request":
            method = _string(event.get("method")) or _string(params.get("method"))
            if method.replace(".", "/") == "thread/read":
                request_params = event.get("params")
                if not isinstance(request_params, Mapping):
                    request_params = params
                # Keep exactly the public selector fields needed for checking.
                safe_request: dict[str, Any] = {}
                for key in ("threadId", "thread_id", "includeTurns", "include_turns"):
                    if key in request_params:
                        safe_request[key] = request_params[key]
                derived.read_requests.append(safe_request)
            continue

        if name == "thread/status/changed":
            if not thread_id:
                derived.errors.append("status event missing thread id")
            else:
                status_value = event.get("status") if "status" in event else params.get("status")
                status = _status_type(status_value)
                _append(derived.thread_statuses, thread_id, status)
                _append(derived.thread_status_events, thread_id, (status, elapsed, index))
            continue

        if name == "turn/started":
            if not thread_id:
                derived.errors.append("turn event missing thread id")
            else:
                _append(derived.turn_starts, thread_id, elapsed)
                _append(derived.turn_start_ids, thread_id, _turn_id(event))
                _append(derived.turn_start_orders, thread_id, index)
            continue

        if name in ("turn/completed", "turn/failed"):
            if not thread_id:
                derived.errors.append("turn event missing thread id")
            else:
                turn = _turn_from_event(event)
                sibling_present = "status" in event or "status" in params
                sibling_status = (event.get("status") if "status" in event else
                                  params.get("status"))
                if (turn is not None and "status" in turn and sibling_present
                        and sibling_status is not None and turn.get("status") is not None):
                    sibling_type = _status_type(sibling_status)
                    nested_type = _status_type(turn.get("status"))
                    if sibling_type is None or nested_type is None or sibling_type != nested_type:
                        derived.errors.append("turn status fields conflict")
                status = _turn_status(event)
                _append(derived.turn_statuses, thread_id, status)
                _append(derived.turn_ends, thread_id, elapsed)
                _append(derived.turn_end_ids, thread_id, _turn_id(event))
                _append(derived.turn_end_orders, thread_id, index)
                _append(derived.turn_end_events, thread_id, name)
                derived.turn_end_order[thread_id] = index
            continue

        if name in ("item/started", "item/completed"):
            item = _item_from_event(event)
            if item is None:
                derived.errors.append("item event missing item")
                continue
            item_type = _string(item.get("type"))
            if item_type == "subAgentActivity":
                child = _string(_first(item, "agentThreadId", "agent_thread_id"))
                if child:
                    derived.child_ids.add(child)
            elif item_type == "collabAgentToolCall":
                for child in _string_list(_first(item, "receiverThreadIds", "receiver_thread_ids")):
                    derived.child_ids.add(child)
                # The public schema describes model/reasoningEffort as the
                # requested configuration for spawnAgent.  Other collab tools
                # use these fields differently (or leave them null), so keep
                # them only for the spawn-specific binding below.
                if _string(item.get("tool")) == "spawnAgent":
                    def spawn_claim(key: str, label: str) -> str | None:
                        value = item.get(key)
                        if key not in item or value is None:
                            return None
                        if not isinstance(value, str) or not value.strip():
                            derived.errors.append(
                                "spawn requested " + label + " has unsupported public shape"
                            )
                            return None
                        return value

                    derived.spawn_tool_calls.append(
                        {
                            "id": _string(item.get("id")),
                            "model": spawn_claim("model", "model"),
                            "reasoningEffort": spawn_claim("reasoningEffort", "effort"),
                        }
                    )
            elif item_type == "commandExecution":
                derived.selector_invoked = derived.selector_invoked or _bool(item.get("selector_invoked")) or _bool(event.get("selector_invoked"))
                if _bool(item.get("tests_failed")) or _bool(event.get("tests_failed")):
                    derived.tests_failed = True
            if _bool(event.get("tests_failed")):
                derived.tests_failed = True
            independent = _map(event.get("independent_tests"))
            if independent is not None and independent.get("ok") is False:
                derived.tests_failed = True
            continue

        if name == "error":
            derived.errors.append("public error event")

        if _bool(event.get("tests_failed")):
            derived.tests_failed = True

    # The first thread/started notification may precede the effective root
    # event.  Resolve root only after scanning all public events.
    if derived.root_id:
        derived.child_ids.discard(derived.root_id)
    return derived


def _validation_result(derived: _PublicDerived, errors: list[str], overlap: float, windows: dict[str, Any], expected_role: str) -> dict[str, Any]:
    child_ids = sorted(derived.child_ids)
    # Keep error text verifier-authored and deterministic.  IDs are public
    # lifecycle identifiers and are safe to retain in diagnostic keys.
    unique_errors = sorted(set(error for error in errors if isinstance(error, str) and error))
    return {
        "ok": not unique_errors,
        "errors": unique_errors,
        "root_id": derived.root_id,
        "root_client_effective": _safe_effective(derived.root_effective),
        "native_child_ids": child_ids,
        "child_runtime_windows": {key: windows[key] for key in sorted(windows)},
        "overlap_seconds": overlap,
        "public_thread_reads": len(derived.read_requests),
        "selector_invoked": derived.selector_invoked,
    }


def validate_public_events(events: list[dict[str, Any]], expected_role: str = DEFAULT_EXPECTED_ROLE, *,
                           requested_root_model: str | None = None,
                           requested_root_effort: str | None = None) -> dict[str, Any]:
    """Validate normalized public lifecycle evidence with strict lineage.

    Callers should pass the already-sanitized event list emitted by the
    transport.  The function never returns any input object, text, command,
    message, reasoning, session, or authentication field.
    """

    derived = _derive_public(events)
    errors = list(derived.errors)
    expected = expected_role if isinstance(expected_role, str) and expected_role else DEFAULT_EXPECTED_ROLE
    if ROLE_RE.fullmatch(expected) is None:
        errors.append("expected role is not adaptive_luna")

    effective = derived.root_effective
    effective_model = effective.get("model")
    if requested_root_model is not None and effective_model != requested_root_model:
        errors.append("root client effective model differs from requested model")
    effort_values = [effective[key] for key in ("reasoningEffort", "reasoning_effort") if key in effective]
    observed_efforts = [value for value in effort_values if isinstance(value, str)]
    if len(set(observed_efforts)) > 1:
        errors.append("root client effort fields conflict")
    reasoning = _string(effective.get("reasoningEffort")) or _string(effective.get("reasoning_effort"))
    if requested_root_effort is not None and reasoning != requested_root_effort:
        errors.append("root client effective effort differs from requested effort")
    claimed_root_model = derived.root_requested.get("model")
    if claimed_root_model is not None and effective_model != claimed_root_model:
        errors.append("root client effective model differs from public requested model")
    requested_effort_values = [derived.root_requested[key] for key in ("reasoningEffort", "reasoning_effort")
                               if key in derived.root_requested and derived.root_requested[key] is not None]
    if requested_effort_values and any(value != reasoning for value in requested_effort_values):
        errors.append("root client effective effort differs from public requested effort")
    request_providers = [derived.root_requested[key] for key in ("modelProvider", "provider")
                         if key in derived.root_requested and derived.root_requested[key] is not None]
    if request_providers and any(value != "openai" for value in request_providers):
        errors.append("root client requested provider is not openai")
    provider = _string(effective.get("modelProvider")) or _string(effective.get("provider"))
    providers = [effective[key] for key in ("modelProvider", "provider")
                 if key in effective and effective[key] is not None]
    if not providers or any(value != "openai" for value in providers) or provider != "openai":
        errors.append("root client provider is not openai")
    if not derived.root_id:
        errors.append("missing root thread id")

    expected_effort = expected.removeprefix("adaptive_luna_")
    for call in derived.spawn_tool_calls:
        # Missing/null values remain compatible with older public streams.  A
        # present, nonempty string is evidence and must agree with the selected role.
        if call["model"] is not None and call["model"] != "gpt-6-luna":
            errors.append("spawn requested model is not gpt-6-luna")
        if call["reasoningEffort"] is not None and call["reasoningEffort"] != expected_effort:
            errors.append("spawn requested effort mismatch")

    root_turns = derived.turn_statuses.get(derived.root_id, [])
    if not root_turns or root_turns[-1] != "completed":
        errors.append("root turn did not complete successfully")
    if any(status != "completed" for status in root_turns):
        errors.append("root has a failed turn status")
    root_starts = derived.turn_starts.get(derived.root_id, [])
    root_ends = derived.turn_ends.get(derived.root_id, [])
    root_start_ids = derived.turn_start_ids.get(derived.root_id, [])
    root_end_ids = derived.turn_end_ids.get(derived.root_id, [])
    root_start_orders = derived.turn_start_orders.get(derived.root_id, [])
    root_end_orders = derived.turn_end_orders.get(derived.root_id, [])
    root_end_events = derived.turn_end_events.get(derived.root_id, [])
    if (len(root_starts) != 1 or len(root_ends) != 1 or len(root_turns) != 1
            or len(root_end_events) != 1 or root_end_events != ["turn/completed"]
            or not root_start_ids or root_start_ids != root_end_ids
            or not all(root_start_ids) or len(root_start_orders) != 1 or len(root_end_orders) != 1
            or root_start_orders[0] >= root_end_orders[0]
            or root_starts[0] is None or root_ends[0] is None or root_ends[0] <= root_starts[0]):
        errors.append("root turn start/completion pair is not exact and chronological")

    if len(derived.child_ids) != 2:
        errors.append("expected exactly two native child threads")
    if len(derived.read_requests) != len(derived.child_ids) or len(derived.read_results) != len(derived.child_ids):
        errors.append("thread/read metadata is missing for a child")

    requested: set[str] = set()
    for request in derived.read_requests:
        identifier = _string(_first(request, "threadId", "thread_id"))
        include_value = request.get("includeTurns") if "includeTurns" in request else request.get("include_turns")
        include_present = "includeTurns" in request or "include_turns" in request
        # The protocol spelling is part of the contract.  A snake-case alias
        # is not accepted as proof because it could be a client-side rewrite.
        if (
            not identifier
            or identifier not in derived.child_ids
            or not include_present
            or not _bool(include_value)
            or include_value is not False
            or "includeTurns" not in request
        ):
            errors.append("child thread/read was not requested with includeTurns=false")
        if identifier in requested:
            errors.append("child thread/read was duplicated")
        requested.add(identifier)

    windows: dict[str, dict[str, Any]] = {}
    overlap = 0.0
    intervals: dict[str, list[tuple[float, float]]] = {}
    for identifier in sorted(derived.child_ids):
        metadata = derived.child_metadata.get(identifier)
        if identifier not in derived.read_results:
            errors.append("thread/read result missing for child")
        if derived.metadata_count.get(identifier) != 1:
            errors.append("expected one final child metadata result: " + identifier)
        if metadata is None:
            errors.append("missing child metadata: " + identifier)
        else:
            if _string(metadata.get("id")) != identifier:
                errors.append("thread/read returned the wrong child id")
            if _string(metadata.get("parentThreadId")) != derived.root_id:
                errors.append("child is not a direct child: " + identifier)
            if _string(metadata.get("agentRole")) != expected:
                errors.append("child role mismatch: " + identifier)
            if _status_type(metadata.get("status")) != "idle":
                errors.append("child final public status is not idle: " + identifier)
            spawn = _map(metadata.get("spawn"))
            if spawn is None:
                errors.append("child spawn depth evidence is missing: " + identifier)
            else:
                if _string(spawn.get("parent_thread_id")) != derived.root_id:
                    errors.append("child spawn parent mismatch: " + identifier)
                if _string(spawn.get("agent_role")) != expected:
                    errors.append("child spawn role mismatch: " + identifier)
                depth = spawn.get("depth")
                if isinstance(depth, bool) or _finite_number(depth) != 1:
                    errors.append("child spawn depth is not one: " + identifier)
            expected_effort = expected.removeprefix("adaptive_luna_")
            claim_sources = [metadata, *derived.thread_started_metadata.get(identifier, [])]
            claims = list(claim_sources)
            for source in claim_sources:
                for name in ("effective", "requested"):
                    nested = source.get(name)
                    if isinstance(nested, Mapping):
                        claims.append(nested)
            for claim in claims:
                model = claim.get("model")
                if model is not None and model != "gpt-6-luna":
                    label = "requested" if claim is metadata.get("requested") else "effective"
                    errors.append("child " + label + " model is not gpt-6-luna: " + identifier)
                efforts = [claim[key] for key in ("reasoningEffort", "reasoning_effort")
                           if key in claim and claim[key] is not None]
                if any(value != expected_effort for value in efforts):
                    label = "requested" if claim is metadata.get("requested") else "effective"
                    errors.append("child " + label + " effort mismatch: " + identifier)
                providers = [claim[key] for key in ("modelProvider", "provider")
                             if key in claim and claim[key] is not None]
                if any(value != "openai" for value in providers):
                    errors.append("child provider is not openai: " + identifier)
            # A later read cannot rewrite contradictory public birth metadata.
            for started in derived.thread_started_metadata.get(identifier, []):
                for key in ("id", "parentThreadId", "agentRole"):
                    first, final = started.get(key), metadata.get(key)
                    if first is not None and final is not None and first != final:
                        errors.append("child thread/started conflicts with final metadata: " + identifier)
                first_spawn, final_spawn = _map(started.get("spawn")), _map(metadata.get("spawn"))
                if first_spawn is not None and final_spawn is not None:
                    for key in ("parent_thread_id", "agent_role", "depth"):
                        first, final = first_spawn.get(key), final_spawn.get(key)
                        if first is not None and final is not None and first != final:
                            errors.append("child thread/started spawn conflicts with final metadata: " + identifier)
            statuses = derived.thread_statuses.get(identifier, [])
            if not statuses or statuses[-1] != "idle":
                errors.append("child has no final idle status event: " + identifier)
            if any(status not in ("active", "idle") for status in statuses):
                errors.append("child reported a public error status: " + identifier)

        statuses = derived.thread_statuses.get(identifier, [])
        starts = derived.turn_starts.get(identifier, [])
        ends = derived.turn_ends.get(identifier, [])
        start_ids = derived.turn_start_ids.get(identifier, [])
        end_ids = derived.turn_end_ids.get(identifier, [])
        start_orders = derived.turn_start_orders.get(identifier, [])
        end_orders = derived.turn_end_orders.get(identifier, [])
        turn_statuses = derived.turn_statuses.get(identifier, [])
        if not turn_statuses or turn_statuses[-1] != "completed":
            errors.append("child turn did not complete: " + identifier)
        if any(status != "completed" for status in turn_statuses):
            errors.append("child has a failed turn status: " + identifier)
        paired = (bool(starts) and len(starts) == len(ends) == len(turn_statuses)
                  and start_ids == end_ids and all(start_ids)
                  and derived.turn_end_events.get(identifier, []) == ["turn/completed"] * len(turn_statuses)
                  and len(set(start_ids)) == len(start_ids))
        if not paired:
            errors.append("child turn start/completion pair is not exact: " + identifier)
        if derived.metadata_order.get(identifier, -1) <= derived.turn_end_order.get(identifier, -1):
            errors.append("child metadata was read before turn completion: " + identifier)

        # A root may ask the same two children to revise their work. Bind every
        # turn by its unique id and preserve the gaps between completed turns.
        child_intervals: list[tuple[float, float]] = []
        turn_windows: list[dict[str, Any]] = []
        if paired:
            for number, (start, end) in enumerate(zip(starts, ends)):
                if start is None or end is None:
                    errors.append("child runtime window is missing: " + identifier)
                    continue
                if (end <= start or end_orders[number] <= start_orders[number]
                    or (number and (ends[number-1] is None or start < ends[number-1]
                                    or start_orders[number] <= end_orders[number-1]))):
                    errors.append("child runtime window is not chronological: " + identifier)
                    continue
                child_intervals.append((start, end))
                turn_windows.append(dict(turn_id=start_ids[number], start_elapsed=start, end_elapsed=end))
        intervals[identifier] = child_intervals
        start = starts[-1] if starts else None
        end = ends[-1] if ends else None
        windows[identifier] = dict(start_elapsed=starts[0] if starts else None,
                                   end_elapsed=end, turns=turn_windows)

        status_events = derived.thread_status_events.get(identifier, [])
        idle_events = [entry for entry in status_events if entry[0] == "idle"]
        active_events = [entry for entry in status_events if entry[0] == "active"]
        # Codex may emit idle just before turn/completed. Require idle to belong
        # to the final turn, plus completed turns and a later idle metadata read;
        # do not impose an undocumented ordering between those two notifications.
        if idle_events and start_orders and idle_events[-1][2] <= start_orders[-1]:
            errors.append("child final idle status preceded final turn: " + identifier)
        if start is not None and active_events and active_events[-1][1] is not None and active_events[-1][1] > start:
            errors.append("child active status followed turn start: " + identifier)
        if start is not None and idle_events and idle_events[-1][1] is not None and idle_events[-1][1] < start:
            errors.append("child final idle status preceded final turn: " + identifier)
        if end is not None and metadata is not None:
            # Metadata events produced by this verifier carry elapsed time in
            # live runs.  Offline callers may omit it; event order remains the
            # required fallback and is checked above.
            metadata_event = next(
                (event for event in events if isinstance(event, Mapping) and _event_name(event) == "child/public-metadata" and _string((_thread_from_event(event) or {}).get("id")) == identifier),
                None,
            ) if isinstance(events, list) else None
            metadata_elapsed = _event_elapsed(metadata_event) if isinstance(metadata_event, Mapping) else None
            if metadata_elapsed is not None and metadata_elapsed < end:
                errors.append("child metadata preceded turn completion: " + identifier)

    if len(intervals) == 2 and all(intervals.values()):
        left, right = intervals.values()
        overlap = sum(max(0.0, min(a_end, b_end) - max(a_start, b_start))
                      for a_start, a_end in left for b_start, b_end in right)
        if overlap <= 0:
            errors.append("native child runtime windows do not overlap")
    else:
        errors.append("child runtime window is missing")
    if derived.tests_failed:
        errors.append("independent smoke tests reported failure")

    result = _validation_result(derived, errors, overlap, windows, expected)
    result["root_requested"] = {"model": requested_root_model, "reasoningEffort": requested_root_effort,
                                "modelProvider": "openai", "allowProviderModelFallback": False}
    return result


def validate_public_evidence(events: list[dict[str, Any]], expected_role: str = DEFAULT_EXPECTED_ROLE) -> dict[str, Any]:
    """Backward-compatible descriptive alias for public event validation."""

    return validate_public_events(events, expected_role)


# The Python transport is owned by the main agent so its process lifecycle can
# be reviewed alongside the platform implementation.  Keep this forwarding
# function tiny and import lazily to avoid a module cycle during CLI startup.
def run(home: Path, codex: str, project: Path, output: Path, role: str | None = None, *,
        root_model: str | None = None, root_effort: str | None = None, select_role=None) -> dict[str, Any]:
    from . import transport
    return transport.run(Path(home), str(codex), Path(project), Path(output), role,
                         root_model=root_model, root_effort=root_effort, select_role=select_role)
