"""Daily public-evidence policy for native Luna role selection.

This module deliberately has no model or authentication integration.  It only
reads the public capability snapshot, fetches the two public radar documents,
and keeps a bounded, per-day policy cache.  All subprocesses and file writes go
through :mod:`codex_adaptive_agents.platform` so the command remains a Python stdlib
runtime on macOS, Linux, and Windows.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time as _clock
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import platform as _platform


VERSION = "0.8.0"
RESOURCE = "codex-adaptive-agents"
CHILD_MODEL = "gpt-6-luna"
MODEL_DIAL_URL = "https://modeldial.com/api/v1/radar/latest.json"
DENG_URL = "https://api.codexradar.com/api/v1/intelligence-efficiency?v=20260823-trend-cohort-v1"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_AGE = timedelta(hours=72)
CAPABILITY_MAX_AGE = timedelta(hours=72)
SOURCE_TIMEOUT = 5.0
RUNTIME_ROUTE = "codex_official_login"
CATEGORY = "backend"


class PolicyError(ValueError):
    """A public, source-safe policy error."""


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise PolicyError("invalid policy time")
    if value.tzinfo is None:
        # Go callers historically supplied a wall clock value.  Treat it as
        # UTC rather than silently using the host's local timezone.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _time(value: object, *, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise PolicyError("invalid policy timestamp")
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise PolicyError("invalid policy timestamp") from None
    else:
        raise PolicyError("invalid policy timestamp")
    if result.tzinfo is None:
        raise PolicyError("invalid policy timestamp")
    return result.astimezone(timezone.utc)


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "0001-01-01T00:00:00Z"
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _strict_json(raw: bytes | str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PolicyError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise PolicyError("non-finite JSON value")

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except PolicyError:
        raise
    except (UnicodeError, TypeError, ValueError):
        raise PolicyError("invalid JSON") from None


def _encode(value: object) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise PolicyError("invalid policy JSON") from None


def _number(value: object, *, finite: bool = True) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if finite and (not math.isfinite(float(value))):
        return None
    return value


def _finite(value: object) -> bool:
    return _number(value) is not None


def _copy(value: object) -> object:
    return deepcopy(value)


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return _stamp(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _cfg() -> dict:
    return {
        "quality_tolerance": 1.0,
        "min_speedup": 0.20,
        "max_age_seconds": int(MAX_AGE.total_seconds()),
        "min_samples": 3,
        "fallback_effort": "xhigh",
        "child_model": CHILD_MODEL,
        "supported_efforts": list(EFFORTS),
        "category": CATEGORY,
        "runtime_route": RUNTIME_ROUTE,
    }


def config_key(config: dict | None = None) -> str:
    """Return the stable identity used by the daily cache."""
    config = _cfg() if config is None else dict(config)
    raw = json.dumps(_jsonable(config), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return "policy-config-v1-" + hashlib.sha256(raw).hexdigest()[:24]


def _max_age(config: dict) -> timedelta:
    value = config.get("max_age_seconds", int(MAX_AGE.total_seconds()))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return timedelta(seconds=-1)
    return timedelta(seconds=float(value))


def _fresh(now: datetime, value: object, age: timedelta, *, future_slack: timedelta = timedelta(minutes=5)) -> bool:
    stamp = _time(value)
    if stamp is None or age <= timedelta(0):
        return False
    return stamp <= now + future_slack and now - stamp <= age


def _cache_fresh(now: datetime, value: object, age: timedelta) -> bool:
    stamp = _time(value)
    if stamp is None or age <= timedelta(0):
        return False
    return stamp <= now and now - stamp <= age


def _supported(effort: object, config: dict) -> bool:
    return isinstance(effort, str) and effort in EFFORTS and effort in config.get("supported_efforts", [])


def _valid_efforts(efforts: object) -> bool:
    if not isinstance(efforts, list) or not efforts:
        return False
    return all(isinstance(e, str) and e in EFFORTS for e in efforts) and len(efforts) == len(set(efforts))


def _canonical_efforts(efforts: list[str]) -> list[str]:
    return [effort for effort in EFFORTS if effort in efforts]


def _source(source: str, status: str = "unavailable", *, url: str = "", now: datetime | None = None,
            problems: list[str] | None = None, rows: list[dict] | None = None, batch: str = "") -> dict:
    return {
        "published_at": "0001-01-01T00:00:00Z",
        "source": source,
        "status": status,
        "url": url,
        "batch": batch,
        "fetched_at": _stamp(now),
        "rows": rows or [],
        "problems": problems or [],
    }


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_time(value: object) -> str:
    return _stamp(_time(value, required=True))


def parse_modeldial(raw: bytes, now: datetime | None = None) -> dict:
    """Parse the public ModelDial latest snapshot without retaining metadata."""
    now = _utc(now)
    result = _source("modeldial", "degraded", url=MODEL_DIAL_URL, now=now,
                     problems=[
                         "CC BY 4.0: ModelDial Public Radar; preserve original rank/order",
                         "publishedAt is publication time; per-evaluation timestamp and repetition count are not supplied by latest.json; automatic downgrade disabled",
                     ])
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise PolicyError("modeldial schema unsupported or incomplete")
    batch = value.get("batch")
    rankings = value.get("rankings")
    schema = value.get("schemaVersion")
    if schema != "1.1" or not isinstance(batch, dict) or not isinstance(rankings, list) or not rankings:
        raise PolicyError("modeldial schema unsupported or incomplete")
    batch_id = batch.get("id")
    if not isinstance(batch_id, str) or not batch_id:
        raise PolicyError("modeldial schema unsupported or incomplete")
    published = _source_time(batch.get("publishedAt"))
    result["batch"] = batch_id
    result["published_at"] = published
    digest = _hash(raw)
    rows = []
    for item in rankings:
        if not isinstance(item, dict):
            raise PolicyError("modeldial schema unsupported or incomplete")
        if item.get("model") != CHILD_MODEL:
            continue
        score = item.get("score")
        maximum = item.get("maxScore", 0)
        if score is not None and (not _finite(score) or not _finite(maximum) or maximum <= 0 or score < 0 or score > maximum):
            raise PolicyError("modeldial numeric schema invalid")
        row = {
            "cohort_updated_at": "0001-01-01T00:00:00Z",
            "task_samples": None,
            "source": "modeldial", "url": MODEL_DIAL_URL,
            "fetched_at": _stamp(now), "evaluated_at": "0001-01-01T00:00:00Z",
            "batch": batch_id, "model": item.get("model"),
            "effort": item.get("reasoningEffort"), "category": item.get("scoreBasis"),
            "protocol": batch.get("evaluationProfile"), "grader": batch.get("graderVersion"),
            "dataset_version": batch.get("questionPackVersion"), "route": item.get("route"),
            "score": score, "score_max": maximum,
            "duration_ms": item.get("elapsedMs"), "duration_metric": "benchmark_suite_elapsed_ms",
            "samples": None, "published_rank": item.get("rank"),
            "critical_failure": False, "content_hash": digest,
        }
        rows.append(row)
    result["rows"] = rows
    if not rows:
        result["problems"].append("Luna rows unavailable")
    return result


def parse_deng(raw: bytes, now: datetime | None = None) -> dict:
    """Parse the public rolling-cohort efficiency snapshot."""
    now = _utc(now)
    result = _source("deng", "degraded", url=DENG_URL, now=now,
                     problems=[
                         "crowdsourced rolling cohort: no shared evaluation batch, grader version or runtime route is provided; automatic downgrade disabled",
                         "iq is publisher's 150-point scale, not combined with ModelDial scores; total is task sample count, not independent repetitions",
                     ])
    value = _strict_json(raw)
    if not isinstance(value, dict):
        raise PolicyError("deng schema unsupported or incomplete")
    points = value.get("points")
    if value.get("schema") != 3 or value.get("mode") != "equal_latest_3" or not isinstance(value.get("benchmark_id"), str) or not value.get("benchmark_id") or not isinstance(points, list) or not points:
        raise PolicyError("deng schema unsupported or incomplete")
    digest = _hash(raw)
    updated = _source_time(value.get("source_updated_at"))
    result["published_at"] = updated
    result["batch"] = "snapshot-sha256:" + digest
    rows = []
    for item in points:
        if not isinstance(item, dict):
            raise PolicyError("deng schema unsupported or incomplete")
        if item.get("model") != CHILD_MODEL:
            continue
        score = item.get("iq")
        if score is not None and (not _finite(score) or score < 0 or score > 150):
            raise PolicyError("deng numeric schema invalid")
        minutes = item.get("average_minutes")
        duration = None
        if minutes is not None:
            if not _finite(minutes) or minutes < 0:
                raise PolicyError("deng duration invalid")
            duration = float(minutes) * 60000
        row_updated = _source_time(item.get("source_updated_at"))
        rows.append({
            "cohort_updated_at": row_updated, "task_samples": item.get("total"),
            "source": "deng", "url": DENG_URL, "fetched_at": _stamp(now),
            "evaluated_at": "0001-01-01T00:00:00Z", "batch": result["batch"],
            "model": item.get("model"), "effort": item.get("effort"), "category": "backend",
            "protocol": str(value.get("mode")) + ":" + str(value.get("scoring_mode")),
            "grader": "unknown", "dataset_version": value.get("benchmark_id"),
            "route": "unknown", "score": score, "score_max": 150,
            "duration_ms": duration, "duration_metric": "rolling_cohort_average_task_elapsed_ms",
            "samples": None, "published_rank": None, "critical_failure": False,
            "content_hash": digest,
        })
    result["rows"] = rows
    return result


def _valid_url(raw: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(raw)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == "https" and not parsed.username and not parsed.password and
            (port in (None, 443)) and hostname in {"modeldial.com", "api.codexradar.com"})


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _valid_url(newurl) or urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            raise urllib.error.URLError("public source redirect rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_public(url: str, *, deadline: float | None = None) -> bytes:
    if not _valid_url(url):
        raise PolicyError("source URL rejected")
    if deadline is None:
        deadline = _clock.monotonic() + SOURCE_TIMEOUT
    opener = urllib.request.build_opener(_RedirectPolicy)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; CodexAdaptiveOrchestrator/0.4; public-read-only)",
        "Accept": "application/json",
    })
    for attempt in range(2):
        remaining = deadline - _clock.monotonic()
        if remaining <= 0:
            raise PolicyError("public request timed out")
        try:
            with opener.open(request, timeout=min(SOURCE_TIMEOUT, remaining)) as response:
                if response.status != 200:
                    if response.status >= 500 and attempt == 0:
                        continue
                    raise PolicyError("public source HTTP status unavailable")
                # urllib's default read() can wait forever on a slow-drip
                # response because each successful byte resets the socket
                # timeout. Read in bounded chunks and refresh the socket
                # timeout from one shared deadline instead.
                parts = []
                size = 0
                while size <= MAX_SOURCE_BYTES:
                    remaining = deadline - _clock.monotonic()
                    if remaining <= 0:
                        raise PolicyError("public request timed out")
                    raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if raw_socket is not None:
                        try:
                            raw_socket.settimeout(remaining)
                        except (AttributeError, OSError):
                            pass
                    read_size = min(16384, MAX_SOURCE_BYTES + 1 - size)
                    try:
                        read = response.read1(read_size) if hasattr(response, "read1") else response.read(read_size)
                    except (socket.timeout, TimeoutError):
                        raise PolicyError("public request timed out") from None
                    if not read:
                        break
                    parts.append(read)
                    size += len(read)
                    if size > MAX_SOURCE_BYTES:
                        raise PolicyError("public response exceeds size bound")
                return b"".join(parts)
        except urllib.error.HTTPError as error:
            if error.code >= 500 and attempt == 0:
                continue
            raise PolicyError("public source unavailable") from None
        except PolicyError:
            raise
        except (OSError, urllib.error.URLError):
            raise PolicyError("public request unavailable") from None
    raise PolicyError("public source unavailable")


def fetch_sources(now: datetime | None = None, budget: float = SOURCE_TIMEOUT) -> list[dict]:
    """Fetch and parse both public sources concurrently under one deadline."""
    now = _utc(now)
    budget = float(budget) if isinstance(budget, (int, float)) else SOURCE_TIMEOUT
    if budget <= 0 or budget > 30:
        budget = SOURCE_TIMEOUT
    jobs = {
        "modeldial": (MODEL_DIAL_URL, parse_modeldial),
        "deng": (DENG_URL, parse_deng),
    }
    output = {}
    guard = threading.Lock()
    deadline = _clock.monotonic() + budget

    def worker(name: str, url: str, parser) -> None:
        try:
            raw = _fetch_public(url, deadline=deadline)
            parsed = parser(raw, now)
        except Exception:
            parsed = _source(name, url=url, now=now,
                             problems=["public source unavailable"])
        with guard:
            output[name] = parsed

    # The worker threads are daemonized deliberately. A broken test double or
    # a third-party urllib implementation must not make the selector wait
    # beyond its shared public-source budget or hold interpreter shutdown.
    threads = [threading.Thread(target=worker, args=(name, url, parser),
                                name="policy-source-" + name, daemon=True)
               for name, (url, parser) in jobs.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - _clock.monotonic()))
    for name, (url, _parser) in jobs.items():
        output.setdefault(name, _source(name, url=url, now=now,
                                        problems=["public source unavailable"]))
    return [output["modeldial"], output["deng"]]


# Go callers and older adapters use this spelling. Keep a wrapper rather than a
# second bound alias so tests and embedders can replace either public spelling.
def FetchSources(now: datetime | None = None, budget: float = SOURCE_TIMEOUT) -> list[dict]:
    return fetch_sources(now, budget)


def _measurement_complete(measurement: dict, now: datetime, config: dict) -> bool:
    score = measurement.get("score")
    maximum = measurement.get("score_max")
    return (
        measurement.get("model") == CHILD_MODEL and
        _supported(measurement.get("effort"), config) and
        measurement.get("category") == config.get("category") and
        measurement.get("route") == config.get("runtime_route") and
        bool(measurement.get("batch")) and bool(measurement.get("protocol")) and
        bool(measurement.get("grader")) and bool(measurement.get("dataset_version")) and
        bool(measurement.get("content_hash")) and
        measurement.get("protocol") != "unknown" and measurement.get("grader") != "unknown" and
        measurement.get("dataset_version") != "unknown" and measurement.get("route") != "unknown" and
        _fresh(now, measurement.get("evaluated_at"), _max_age(config)) and
        _finite(score) and _finite(maximum) and maximum > 0 and 0 <= score <= maximum and
        measurement.get("critical_failure") is not True
    )


def _group_key(measurement: dict) -> tuple:
    return tuple(measurement.get(k) for k in (
        "source", "batch", "category", "protocol", "grader", "dataset_version", "route", "score_max", "duration_metric"))


def _score(measurement: dict) -> float:
    return float(measurement["score"]) / float(measurement["score_max"]) * 100


def _speed_evidence(measurement: dict, config: dict) -> bool:
    duration = measurement.get("duration_ms")
    samples = measurement.get("samples")
    return (_finite(duration) and duration > 0 and bool(measurement.get("duration_metric")) and
            measurement.get("duration_metric") != "unknown" and isinstance(samples, int) and not isinstance(samples, bool) and
            samples >= config.get("min_samples", 3))


def _preserved_previous(now: datetime, previous: dict | None, config: dict) -> bool:
    if not isinstance(previous, dict) or not _supported(previous.get("default_effort"), config):
        return False
    if not _fresh(now, previous.get("generated_at"), _max_age(config), future_slack=timedelta(0)):
        return False
    if previous.get("runtime_route") != config.get("runtime_route") or previous.get("status") not in {"valid", "degraded"}:
        return False
    evidence = previous.get("selected_evidence")
    return isinstance(evidence, list) and bool(evidence) and all(_measurement_complete(m, now, config) for m in evidence if isinstance(m, dict)) and all(isinstance(m, dict) for m in evidence)


def _decision(now: datetime, sources: list[dict], config: dict, previous: dict | None = None) -> dict:
    fallback = config.get("fallback_effort", "xhigh")
    decision = {
        "version": "",
        "generated_at": _stamp(now),
        "default_effort": fallback,
        "status": "fallback",
        "reasons": [],
        "sources": _copy(sources),
        "selected_evidence": [],
        "quality_effort": "",
        "conflict": False,
        "runtime_route": config.get("runtime_route", RUNTIME_ROUTE),
    }
    quality = config.get("quality_tolerance", 1)
    speedup = config.get("min_speedup", .20)
    min_samples = config.get("min_samples", 3)
    if (not _finite(quality) or quality < 0 or quality > 100 or not _finite(speedup) or speedup < 0 or speedup >= 1 or
            not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 1 or not config.get("category") or not config.get("runtime_route")):
        raise PolicyError("invalid policy thresholds")
    if not _supported(fallback, config):
        decision["status"] = "blocked"
        decision["default_effort"] = ""
        raise PolicyError("configured fallback effort unavailable: " + str(fallback))
    sources = [_copy(v) for v in sources if isinstance(v, dict)]
    decision["sources"] = sources
    degraded = len(sources) < 2
    counts = {}
    for source in sources:
        counts[source.get("source", "")] = counts.get(source.get("source", ""), 0) + 1
    choices = []
    for source in sources:
        name = source.get("source", "")
        if counts.get(name, 0) > 1:
            degraded = True
            decision["reasons"].append(name + ": multiple source snapshots; batches are not combined")
            continue
        if source.get("status") != "ok":
            degraded = True
        if source.get("status") == "unavailable":
            decision["reasons"].append(name + ": unavailable")
            continue
        groups = {}
        for measurement in source.get("rows", []) or []:
            if not isinstance(measurement, dict) or measurement.get("source") != name or measurement.get("batch") != source.get("batch") or not _measurement_complete(measurement, now, config):
                continue
            groups.setdefault(_group_key(measurement), []).append(measurement)
        if len(groups) != 1:
            decision["reasons"].append(name + ": no single complete, fresh group matching runtime route/category")
            degraded = True
            continue
        rows = next(iter(groups.values()))
        seen = set()
        ambiguous = False
        for measurement in rows:
            if measurement.get("effort") in seen:
                ambiguous = True
            seen.add(measurement.get("effort"))
        if ambiguous:
            decision["reasons"].append(name + ": duplicate effort evidence; no automatic selection")
            degraded = True
            continue
        best = rows[0]
        for measurement in rows:
            if _score(measurement) > _score(best):
                best = measurement
        selected = best
        if _speed_evidence(best, config):
            for measurement in rows:
                if (_score(best) - _score(measurement) <= quality + 1e-9 and _speed_evidence(measurement, config) and
                        1 - float(measurement["duration_ms"]) / float(best["duration_ms"]) >= speedup - 1e-9 and
                        (selected.get("effort") == best.get("effort") or measurement["duration_ms"] < selected.get("duration_ms", float("inf")))):
                    selected = measurement
        choices.append({"source": name, "best": best, "selected": selected, "rows": rows})
    choices.sort(key=lambda item: (0 if item["source"] == "modeldial" else 1, item["source"]))
    if choices:
        primary = choices[0]
        best, selected = primary["best"], primary["selected"]
        decision["default_effort"] = selected.get("effort", "")
        decision["quality_effort"] = best.get("effort", "")
        decision["status"] = "degraded" if degraded else "valid"
        decision["selected_evidence"] = [_copy(best)]
        if selected.get("effort") != best.get("effort"):
            decision["selected_evidence"].append(_copy(selected))
        gap = _score(best) - _score(selected)
        decision["reasons"].append(f"{primary['source']}: quality reference {best.get('effort')}; selected {selected.get('effort')}; score gap {gap:.3g}/100")
        if selected.get("duration_ms") is not None and best.get("duration_ms") is not None and best.get("duration_ms") > 0:
            decision["reasons"].append(f"within-source measured time reduction {100 * (1 - selected['duration_ms'] / best['duration_ms']):.2f}%")
        for secondary in choices[1:]:
            for measurement in secondary["rows"]:
                if measurement.get("effort") == decision["default_effort"] and _score(secondary["best"]) - _score(measurement) > max(5, 2 * quality):
                    decision["conflict"] = True
                    decision["reasons"].append(secondary["source"] + ": selected effort substantially below this source's quality reference")
        if decision["conflict"]:
            decision["default_effort"] = fallback
            decision["status"] = "fallback"
            decision["selected_evidence"] = []
            if _preserved_previous(now, previous, config):
                decision["default_effort"] = previous["default_effort"]
                decision["status"] = "degraded"
                decision["selected_evidence"] = _copy(previous["selected_evidence"])
            decision["reasons"].append("source conflict prevents automatic downgrade")
    elif _preserved_previous(now, previous, config):
        decision["default_effort"] = previous["default_effort"]
        decision["status"] = "degraded"
        decision["selected_evidence"] = _copy(previous["selected_evidence"])
        decision["reasons"].append("retained previously validated, still-fresh configuration")
    else:
        decision["reasons"].append("insufficient comparable evidence; explicit local conservative fallback")
    decision["reasons"].append("tolerance and time thresholds are configured heuristics; no statistical equivalence or quality-loss guarantee; task acceptance still required")
    decision["version"] = _decision_version(config, decision)
    return decision


def _decision_version(config: dict, decision: dict) -> str:
    """Recompute the stable digest for a serialized policy decision."""
    evidence = _copy(decision["selected_evidence"])
    identity = {
        "config": _jsonable(config), "effort": decision["default_effort"], "status": decision["status"],
        "conflict": decision["conflict"], "evidence": evidence,
        "batches": [s.get("source", "") + ":" + s.get("batch", "") + ":" + s.get("status", "")
                    for s in decision["sources"]],
    }
    for evidence in identity["evidence"]:
        evidence["fetched_at"] = "0001-01-01T00:00:00Z"
    digest = hashlib.sha256(json.dumps(_jsonable(identity), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:24]
    return "policy-v1-" + digest


def select_policy(now: datetime, sources: list[dict], config: dict | None = None, previous: dict | None = None) -> dict:
    """Pure engine entry point useful to callers and offline tests."""
    return _decision(_utc(now), sources, _cfg() if config is None else config, previous)


def _safe(path: Path) -> None:
    try:
        _platform.safe(path)
    except Exception:
        raise PolicyError("unsafe policy path") from None


def _read(path: Path) -> bytes | None:
    try:
        data = _platform.read(path)
    except Exception:
        raise PolicyError("policy file unavailable") from None
    if data is not None and len(data) > MAX_CACHE_BYTES:
        raise PolicyError("policy cache exceeds size bound")
    return data


def _atomic(path: Path, data: bytes) -> None:
    try:
        _platform.atomic(path, data, 0o600)
    except Exception:
        raise PolicyError("policy cache write failed") from None


def _cache(path: Path) -> dict | None:
    data = _read(path)
    if data is None:
        return None
    try:
        value = _strict_json(data)
    except PolicyError:
        raise PolicyError("policy cache invalid") from None
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1:
        raise PolicyError("policy cache invalid")
    allowed = {"schema", "attempted_days", "last_attempt", "last_success", "capabilities_at", "supported_efforts", "timezone", "day", "last_status", "config_key", "policy"}
    if set(value) - allowed:
        raise PolicyError("policy cache invalid")
    if not isinstance(value.get("attempted_days", {}), dict) or not isinstance(value.get("supported_efforts", []), list) or not isinstance(value.get("policy", {}), dict):
        raise PolicyError("policy cache invalid")
    return value


def _new_cache(now: datetime, config: dict) -> dict:
    decision = _decision(now, [], config)
    return {
        "schema": 1, "attempted_days": {}, "last_attempt": _stamp(None), "last_success": _stamp(None),
        "capabilities_at": _stamp(None), "supported_efforts": [], "timezone": "", "day": "",
        "last_status": decision["status"], "config_key": config_key(config), "policy": decision,
    }


def _state(home: Path, project: Path | None) -> Path:
    home = Path(os.path.expanduser(str(home)))
    if not home.is_absolute():
        raise PolicyError("absolute Codex home required")
    if project is None:
        return home / RESOURCE / "state"
    project = Path(os.path.expanduser(str(project)))
    if not project.is_absolute():
        raise PolicyError("absolute project path required")
    if not project.is_dir() or project.is_symlink():
        raise PolicyError("project must be an existing directory")
    return project / ".codex-adaptive-agents" / "state"


def _snapshot_path(home: Path) -> Path:
    home = Path(os.path.expanduser(str(home)))
    if not home.is_absolute():
        raise PolicyError("absolute Codex home required")
    return home / RESOURCE / "capabilities.json"


def _read_snapshot_document(path: Path) -> dict:
    """Read the structurally valid public snapshot, including probe metadata.

    Freshness is intentionally checked by the caller. A stale but otherwise
    valid snapshot still provides parseable metadata, but its CLI path is only
    provenance and its effort set is never used as fallback after the 72 hour
    TTL.
    """
    data = _read(path)
    if data is None or not data or len(data) > MAX_SOURCE_BYTES:
        raise PolicyError("public capability snapshot unavailable")
    try:
        value = _strict_json(data)
    except PolicyError:
        raise PolicyError("public capability snapshot unavailable") from None
    if not isinstance(value, dict):
        raise PolicyError("public capability snapshot unavailable")
    schema = value.get("schema")
    if type(schema) is int and schema == 1:
        # Accept the previous verified handoff shape, but treat its root_model
        # field as provenance only. Child capability does not depend on root.
        allowed = {"schema", "checked_at", "supported_efforts", "root_model", "child_model", "codex", "cli_version", "evidence"}
        legacy_root = value.get("root_model")
        identity_ok = (isinstance(legacy_root, str) and bool(legacy_root.strip()) and
                       value.get("child_model") == CHILD_MODEL)
    elif type(schema) is int and schema == 2:
        allowed = {"schema", "checked_at", "supported_efforts", "child_model", "codex", "cli_version", "evidence"}
        identity_ok = value.get("child_model") == CHILD_MODEL
    else:
        raise PolicyError("public capability snapshot unavailable")
    if set(value) - allowed or not identity_ok:
        raise PolicyError("public capability snapshot unavailable")
    efforts = value.get("supported_efforts")
    if not _valid_efforts(efforts):
        raise PolicyError("public capability snapshot unavailable")
    checked = _time(value.get("checked_at"), required=True)
    original = datetime.fromisoformat(str(value["checked_at"]).replace("Z", "+00:00"))
    if original.utcoffset() != timedelta(0):
        raise PolicyError("public capability snapshot unavailable")
    codex = value.get("codex")
    if codex is not None and (not isinstance(codex, str) or not codex or not Path(codex).is_absolute()):
        raise PolicyError("public capability snapshot unavailable")
    for field in ("cli_version", "evidence"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            raise PolicyError("public capability snapshot unavailable")
    result = {"schema": 2, "checked_at": _stamp(checked),
              "supported_efforts": _canonical_efforts(efforts), "child_model": CHILD_MODEL}
    # Keep the recorded path as provenance for inspection. Selection resolves
    # its executable from an explicit caller choice or normal PATH.
    if codex:
        result["codex"] = codex
    return result


def _read_snapshot(path: Path, now: datetime) -> dict:
    document = _read_snapshot_document(path)
    checked = _time(document["checked_at"], required=True)
    if checked > now or now - checked > CAPABILITY_MAX_AGE:
        raise PolicyError("public capability snapshot unavailable")
    return document


def _read_snapshot_for_probe(path: Path, now: datetime) -> dict:
    """Return valid snapshot metadata for live probing and optional fallback."""
    document = _read_snapshot_document(path)
    checked = _time(document["checked_at"], required=True)
    if checked > now:
        # A future timestamp is an invalid/malicious snapshot. Do not trust its
        # executable path or turn it into a fresh capability observation.
        raise PolicyError("public capability snapshot unavailable")
    document["fallback_allowed"] = now - checked <= CAPABILITY_MAX_AGE
    return document


def read_capabilities(home: Path, now: datetime | None = None) -> dict:
    """Read and validate the installed public capability snapshot."""
    return _read_snapshot(_snapshot_path(Path(home)), _utc(now))


def _cache_stale(cache: dict, now: datetime, config: dict) -> bool:
    age = _max_age(config)
    return not _cache_fresh(now, cache.get("last_success"), age) or not _cache_fresh(now, cache.get("capabilities_at"), age)


def _future_cache(cache: dict, now: datetime) -> bool:
    for key in ("last_attempt", "last_success", "capabilities_at"):
        stamp = _time(cache.get(key))
        if stamp is not None and stamp > now:
            return True
    policy = cache.get("policy", {})
    generated = _time(policy.get("generated_at")) if isinstance(policy, dict) else None
    if generated is not None and generated > now:
        return True
    for value in (cache.get("attempted_days") or {}).values():
        stamp = _time(value)
        if stamp is not None and stamp > now:
            return True
    return False


def _usable(cache: dict, result_stale: bool, now: datetime, config: dict) -> bool:
    try:
        policy = cache.get("policy", {})
        if not isinstance(policy, dict):
            return False
        status = policy.get("status")
        if (status not in {"valid", "degraded", "fallback"} or
                cache.get("last_status") != status or
                cache.get("config_key") != config_key(config) or _future_cache(cache, now)):
            return False
        age = _max_age(config)
        if not _cache_fresh(now, cache.get("capabilities_at"), age) or not _cache_fresh(now, policy.get("generated_at"), age):
            return False
        efforts = cache.get("supported_efforts")
        if not _valid_efforts(efforts) or efforts != _canonical_efforts(efforts):
            return False
        wanted = policy.get("default_effort")
        if not isinstance(wanted, str) or wanted not in efforts or wanted not in EFFORTS:
            return False
        if not isinstance(policy.get("reasons"), list) or not all(isinstance(item, str) for item in policy["reasons"]):
            return False
        if policy.get("runtime_route") != config.get("runtime_route") or type(policy.get("conflict")) is not bool:
            return False
        sources = policy.get("sources")
        evidence = policy.get("selected_evidence")
        if (not isinstance(sources, list) or
                not all(isinstance(row, dict) and all(isinstance(row.get(key, ""), str)
                    for key in ("source", "batch", "status")) and
                    row.get("source") in {"modeldial", "deng"} and
                    row.get("status") in {"ok", "degraded", "unavailable"}
                    for row in sources) or
                not isinstance(evidence, list)):
            return False
        effective_config = dict(config)
        effective_config["supported_efforts"] = list(efforts)
        quality_effort = policy.get("quality_effort")
        if not isinstance(quality_effort, str) or (quality_effort and quality_effort not in efforts):
            return False
        if status == "fallback":
            if wanted != config.get("fallback_effort") or evidence:
                return False
        else:
            if (not evidence or wanted not in [row.get("effort") for row in evidence if isinstance(row, dict)] or
                    not all(isinstance(row, dict) and row.get("source") in {"modeldial", "deng"} and
                            isinstance(row.get("batch"), str) and bool(row.get("batch")) and
                            _measurement_complete(row, now, effective_config) for row in evidence)):
                return False
            if (not policy.get("conflict") and quality_effort and
                    quality_effort != evidence[0].get("effort")):
                return False
            if status == "valid" and quality_effort != evidence[0].get("effort"):
                return False
            if status == "valid":
                source_names = [row["source"] for row in sources]
                if (len(sources) < 2 or len(set(source_names)) != len(source_names) or
                        any(row["status"] != "ok" for row in sources)):
                    return False
                for row in evidence:
                    if not any(row in source.get("rows", []) and
                               source.get("source") == row.get("source") and
                               source.get("batch") == row.get("batch")
                               for source in sources):
                        return False
            if status == "valid" and policy.get("conflict"):
                return False
        if status != "fallback" and not _cache_fresh(now, cache.get("last_success"), age):
            return False
        if not re.fullmatch(r"policy-v1-[0-9a-f]{24}", str(policy.get("version", ""))):
            return False
        return policy["version"] == _decision_version(effective_config, policy)
    except (AttributeError, KeyError, OverflowError, PolicyError, TypeError, ValueError):
        return False


def _result(cache: dict, now: datetime, config: dict, *, refreshed: bool = False, refresh_in_progress: bool = False) -> dict:
    try:
        stale = _cache_stale(cache, now, config)
    except PolicyError:
        stale = True
    policy = cache.get("policy") if isinstance(cache.get("policy"), dict) else {}
    usable = _usable(cache, stale, now, config)
    effort = policy.get("default_effort", "") if usable else ""
    status = (policy.get("status", "blocked") or "blocked") if usable else "blocked"
    reasons = policy.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        reasons = []
    generated_at = policy.get("generated_at")
    if not isinstance(generated_at, str):
        generated_at = _stamp(now)
    capabilities_at = cache.get("capabilities_at")
    if not isinstance(capabilities_at, str):
        capabilities_at = _stamp(None)
    return {
        "role": "adaptive_luna_" + effort if usable else "",
        "effort": effort,
        "status": status,
        "policy_version": policy.get("version", "") if usable and isinstance(policy.get("version"), str) else "",
        "refreshed": bool(refreshed),
        "stale": stale,
        "reasons": list(reasons),
        "generated_at": generated_at,
        "capabilities_at": capabilities_at,
        "day": cache.get("day", "") if isinstance(cache.get("day", ""), str) else "",
        "timezone": cache.get("timezone", "") if isinstance(cache.get("timezone", ""), str) else "",
        "refresh_in_progress": bool(refresh_in_progress),
    }


def _system_timezone(now: datetime) -> tuple[str, object, str]:
    """Return the host zone label, tzinfo and current local date.

    ``ZoneInfo`` data is present on the Unix builds used by the installer, but
    it is not guaranteed on a stock Windows Python. In that case the operating
    system's aware local datetime and ``time.tzname`` remain authoritative for
    the natural-day boundary; the label is retained in the cache so a zone
    change cannot silently reuse an old day's attempt.
    """
    requested = os.environ.get("TZ", "")
    if requested:
        if requested in ("UTC", "Etc/UTC", "GMT", "Etc/GMT"):
            zone = timezone.utc
        else:
            try:
                zone = ZoneInfo(requested)
            except Exception:
                raise PolicyError("TZ needs available IANA timezone data; unset TZ to use the system local timezone") from None
        local = now.astimezone(zone)
        return requested, zone, local.date().isoformat()

    # On Unix, resolve the system's IANA link when it is available. macOS uses
    # /var/db/timezone/zoneinfo while Linux normally uses /usr/share/zoneinfo.
    if os.name != "nt":
        for path in ("/etc/localtime", "/var/db/timezone/localtime"):
            try:
                target = os.readlink(path)
            except (FileNotFoundError, OSError):
                continue
            marker = "zoneinfo/"
            if marker not in target:
                continue
            name = target.split(marker, 1)[1].lstrip("/")
            if not name or name.startswith(".."):  # do not load an unsafe link target
                continue
            try:
                zone = ZoneInfo(name)
            except Exception:
                continue
            local = now.astimezone(zone)
            return name, zone, local.date().isoformat()

    # Windows (and Unix hosts with a copied /etc/localtime) still supply a
    # correct local offset through astimezone(). Do not pretend that this is
    # UTC merely because a portable IANA database is absent.
    local = now.astimezone()
    zone = local.tzinfo
    name = getattr(zone, "key", None) if zone is not None else None
    if not isinstance(name, str) or not name:
        name = local.tzname() or ""
    if not name:
        names = getattr(_clock, "tzname", ())
        index = 1 if local.dst() else 0
        if isinstance(names, tuple) and names:
            name = names[min(index, len(names) - 1)] or names[0]
    if not name:
        offset = local.utcoffset() or timedelta(0)
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        name = "UTC{}{:02d}:{:02d}".format(sign, seconds // 3600, (seconds % 3600) // 60)
    return "local:" + name, zone or timezone.utc, local.date().isoformat()


def _empty_result(now: datetime, reason: str, *, timezone_name: str = "", day: str = "") -> dict:
    return {
        "role": "", "effort": "", "status": "blocked", "policy_version": "",
        "refreshed": False, "stale": True, "reasons": [reason],
        "generated_at": _stamp(now), "capabilities_at": _stamp(None),
        "day": day, "timezone": timezone_name, "refresh_in_progress": False,
    }


def read_cached(home: Path, project: Path | None = None, *, now: datetime | None = None) -> dict:
    """Read today's cached decision without a source or CLI request.

    A missing, stale, differently-zoned or differently-shaped cache returns a
    blocked result with an explicit refresh instruction. This makes ``--explain``
    safe for offline diagnosis while keeping callers from dispatching a role
    that was not produced for the current natural day.
    """
    now = _utc(now)
    try:
        timezone_name, _zone, local_day = _system_timezone(now)
        state = _state(Path(home), project)
        cache = _cache(state / "policy-cache.json")
    except PolicyError as error:
        return _empty_result(now, str(error))
    if cache is None:
        return _empty_result(now, "policy cache unavailable; run refresh")
    day_key = timezone_name + ":" + local_day
    attempted = cache.get("attempted_days") or {}
    if (cache.get("timezone") != timezone_name or cache.get("day") != local_day or
            day_key not in attempted):
        return _empty_result(now, "policy cache is not current for the local day; run refresh",
                             timezone_name=timezone_name, day=local_day)
    try:
        result = _result(cache, now, _cfg())
    except PolicyError:
        return _empty_result(now, "policy cache invalid; run refresh",
                             timezone_name=timezone_name, day=local_day)
    if not result.get("role"):
        result["reasons"] = list(result.get("reasons") or []) + ["cached policy unavailable or stale; run refresh"]
        result["status"] = "blocked"
    return result


def cached_select(home: Path, project: Path | None = None, *, now: datetime | None = None) -> dict:
    """Return only cached policy state; this helper never fetches a source."""
    return read_cached(home, project, now=now)


def _native_inputs(home: Path, snapshot: dict | None, now: datetime,
                   codex: str | None = None) -> tuple[list[dict], list[str], str, bool]:
    """Fetch sources and the local catalogue concurrently for one refresh.

    The returned capability timestamp is the observation time of the selected
    effort set. A snapshot fallback deliberately returns its original
    ``checked_at`` value, so daily calls cannot extend its TTL.
    """
    output: dict[str, object] = {"sources": None, "efforts": None,
                                 "capabilities_at": None, "fallback": False}
    guard = threading.Lock()
    closed = False
    deadline = _clock.monotonic() + SOURCE_TIMEOUT
    # Capability metadata is data, not authority to execute a path. Callers
    # may pass a CLI already resolved from an explicit option; library callers
    # without one use the normal PATH command.
    codex = codex if isinstance(codex, str) and codex else "codex"

    def put(key: str, value: object) -> None:
        nonlocal closed
        with guard:
            # A worker may still be unwinding a subprocess after the shared
            # budget expires. Once the coordinator closes the result, a late
            # worker must not write half of a capability/source observation.
            if closed or _clock.monotonic() >= deadline:
                return
            output[key] = value

    def put_capability(efforts: list[str]) -> None:
        nonlocal closed
        with guard:
            if closed or _clock.monotonic() >= deadline:
                return
            output.update({"efforts": _canonical_efforts(efforts),
                           "capabilities_at": _stamp(now), "fallback": False})

    def budget_open() -> bool:
        with guard:
            return not closed and _clock.monotonic() < deadline

    def snapshot_fallback() -> tuple[list[str], str] | None:
        """Return only a still-fresh snapshot fallback with its original age."""
        if not isinstance(snapshot, dict) or not snapshot.get("fallback_allowed"):
            return None
        efforts = snapshot.get("supported_efforts")
        checked = snapshot.get("checked_at")
        if not _valid_efforts(efforts) or not isinstance(checked, str):
            return None
        try:
            checked_at = _time(checked, required=True)
        except PolicyError:
            return None
        if checked_at > now or now - checked_at > CAPABILITY_MAX_AGE:
            return None
        return _canonical_efforts(efforts), _stamp(checked_at)

    def source_worker() -> None:
        if not budget_open():
            return
        try:
            remaining = deadline - _clock.monotonic()
            if remaining <= 0:
                return
            put("sources", FetchSources(now, remaining))
        except Exception:
            put("sources", None)

    def capability_worker() -> None:
        efforts = None
        if codex and budget_open():
            try:
                # Call the exported function so tests/adapters can replace the
                # public capability probe without touching subprocess internals.
                efforts = DirectSupportedEfforts(home, codex)
            except Exception:
                efforts = None
        if isinstance(efforts, list) and _valid_efforts(efforts):
            # Keep all capability fields as one atomic result. This prevents
            # the coordinator from observing efforts without their timestamp.
            put_capability(efforts)

    threads = [
        threading.Thread(target=source_worker, name="policy-sources", daemon=True),
        threading.Thread(target=capability_worker, name="policy-capabilities", daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        remaining = deadline - _clock.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)

    # Close and snapshot the shared result under the same lock used by worker
    # writes. A capability probe that is still in its inner CLI timeout/cleanup
    # cannot replace the fresh snapshot fallback after this point.
    with guard:
        closed = True
        if not (isinstance(output.get("efforts"), list) and
                _valid_efforts(output.get("efforts"))):
            fallback = snapshot_fallback()
            if fallback is not None:
                output.update({"efforts": fallback[0],
                               "capabilities_at": fallback[1], "fallback": True})
        sources = output.get("sources")
        efforts = output.get("efforts")
        capability_at = output.get("capabilities_at")
        capability_fallback = bool(output.get("fallback"))

    if not isinstance(sources, list):
        sources = [_source("modeldial", url=MODEL_DIAL_URL, now=now,
                           problems=["public source unavailable"]),
                   _source("deng", url=DENG_URL, now=now,
                           problems=["public source unavailable"])]
    if not isinstance(efforts, list) or not _valid_efforts(efforts):
        efforts = []
    if not isinstance(capability_at, str):
        capability_at = _stamp(None)
    return sources, efforts, capability_at, capability_fallback


def select(home: Path, project: Path | None = None, *, force: bool = False,
           now: datetime | None = None, codex: str | None = None) -> dict:
    """Select one fixed Luna role, refreshing at most once per local day."""
    now = _utc(now)
    if os.environ.get("ORCHESTRATOR_WORKER") == "1":
        return {"role": "", "effort": "", "status": "blocked", "policy_version": "", "refreshed": False,
                "stale": True, "reasons": ["selector disabled for worker process"], "generated_at": _stamp(now),
                "capabilities_at": _stamp(None), "day": "", "timezone": "", "refresh_in_progress": False}
    base_config = _cfg()
    state = _state(Path(home), project)
    cache_path = state / "policy-cache.json"
    lock_path = state / "policy-refresh.lock"
    _safe(cache_path)
    cache_invalid = False
    try:
        cache = _cache(cache_path)
    except PolicyError:
        # Ordinary selection must not reset a malformed cache and erase its
        # daily attempt marker. An explicit forced refresh is the repair path.
        if not force:
            return _empty_result(now, "policy cache invalid; run refresh")
        cache = None
        cache_invalid = True
    if cache is None:
        cache = _new_cache(now, base_config)
    timezone_name, _zone, local_day = _system_timezone(now)
    day_key = timezone_name + ":" + local_day
    try:
        with _platform.file_lock(lock_path, timeout=5):
            try:
                latest = _cache(cache_path)
            except PolicyError:
                if not force:
                    return _empty_result(now, "policy cache invalid; run refresh",
                                         timezone_name=timezone_name, day=local_day)
                latest = None
                cache_invalid = True
            if latest is not None:
                cache = latest
                cache_invalid = False
            elif cache_invalid or cache is None:
                cache = _new_cache(now, base_config)
            previous_matches = cache.get("config_key") == config_key(base_config)
            if not previous_matches:
                # A model or policy identity change gets one fresh attempt for
                # this local day. Do not carry supported efforts, capability
                # timestamps, or scored evidence across identities.
                cache = _new_cache(now, base_config)
            # Preserve an earlier decision only if the complete cache is still
            # usable. Capture this before `last_status` becomes `refreshing`.
            previous = (cache.get("policy") if previous_matches and
                        _usable(cache, False, now, base_config) else None)
            attempted = day_key in (cache.get("attempted_days") or {})
            if attempted and not force:
                return _result(cache, now, base_config)
            attempted_days = cache.setdefault("attempted_days", {})
            attempted_days[day_key] = _stamp(now)
            cache.update({"last_attempt": _stamp(now), "timezone": timezone_name, "day": local_day,
                          "last_status": "refreshing", "config_key": config_key(base_config)})
            _atomic(cache_path, _encode(cache))

            snapshot = None
            try:
                # Keep stale metadata available for the next direct probe, but
                # let _native_inputs decide whether its effort set is still a
                # permitted fallback.
                snapshot = _read_snapshot_for_probe(_snapshot_path(Path(home)), now)
            except PolicyError:
                snapshot = None

            # The direct probe and both public source requests share this one
            # daily refresh budget. The snapshot is a capability fallback only;
            # its checked_at remains unchanged in the project cache.
            sources, efforts, capability_at, capability_fallback = _native_inputs(
                Path(home), snapshot, now, codex=codex)
            if efforts:
                cache["supported_efforts"] = list(efforts)
                cache["capabilities_at"] = capability_at
            elif not (_valid_efforts(cache.get("supported_efforts")) and
                      _cache_fresh(now, cache.get("capabilities_at"), _max_age(base_config))):
                cache["supported_efforts"] = []
                cache["capabilities_at"] = _stamp(None)

            config = dict(base_config)
            capability_fresh = (_cache_fresh(now, cache.get("capabilities_at"), _max_age(config)) and
                                _valid_efforts(cache.get("supported_efforts")))
            if capability_fresh:
                config["supported_efforts"] = list(cache["supported_efforts"])
            try:
                decision = _decision(now, sources, config, previous)
            except PolicyError as error:
                decision = {"version": "", "generated_at": _stamp(now), "default_effort": "", "status": "blocked",
                            "reasons": [str(error) + "; automatic dispatch blocked"], "sources": sources,
                            "selected_evidence": [], "quality_effort": "", "conflict": False, "runtime_route": RUNTIME_ROUTE}
            if not capability_fresh:
                decision["status"] = "blocked"
                decision["version"] = (decision.get("version") or "policy-v1-") + "-capabilities-unavailable"
                decision["reasons"] = list(decision.get("reasons") or []) + ["local supported efforts unavailable or stale; automatic dispatch blocked"]
            if capability_fallback:
                decision["reasons"] = list(decision.get("reasons") or []) + ["live local model catalog unavailable; used fresh public capability snapshot"]
            cache["policy"] = decision
            cache["last_status"] = decision.get("status", "blocked")
            for source in sources:
                if source.get("status") != "unavailable" and source.get("rows"):
                    cache["last_success"] = _stamp(now)
                    break
            _atomic(cache_path, _encode(cache))
            # Cache identity tracks caller policy inputs, not capability
            # observations. Keep the base config here so a partial live
            # catalogue cannot make the same-day cache look parameter-stale.
            return _result(cache, now, base_config, refreshed=True)
    except RuntimeError:
        return _result(cache, now, base_config, refresh_in_progress=True)


def select_for_root(home: Path, project: Path | None, root_model: str | None,
                    *, now: datetime | None = None) -> dict:
    """Compatibility spelling; ``root_model`` does not affect selection."""
    return select(home, project, now=now)


_VERSION_RE = re.compile(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?\s*$")


def _run_codex(home: Path, argv: list[str], *, timeout: float = 5, limit: int = MAX_SOURCE_BYTES):
    env = _platform.clean_env()
    # CODEX_HOME is a routing variable, not a credential.  The official CLI
    # reads its own login state; this module never opens that state.
    env.pop("ORCHESTRATOR_WORKER", None)
    env["CODEX_HOME"] = str(Path(home))
    try:
        result = _platform.run(argv, env=env, timeout=timeout, limit=limit)
    except Exception:
        raise PolicyError("official Codex CLI command unavailable") from None
    if getattr(result, "returncode", 0) != 0:
        raise PolicyError("official Codex CLI command unavailable")
    return result


def _stdout(result) -> bytes:
    value = getattr(result, "stdout", b"")
    if isinstance(value, str):
        return value.encode()
    return bytes(value or b"")


def _check_cli_version(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise PolicyError("Codex CLI version unavailable") from None
    match = _VERSION_RE.fullmatch(text)
    if not match or tuple(map(int, match.groups())) < (0, 147, 0):
        raise PolicyError("Codex CLI 0.147.0+ required")
    return text


def _catalog_efforts(raw: bytes, *, require_full: bool = False) -> list[str]:
    value = _strict_json(raw)
    if not isinstance(value, dict) or set(value) != {"models"} or not isinstance(value.get("models"), list):
        raise PolicyError("unsupported public model catalogue schema")
    luna_levels = None
    for item in value["models"]:
        if not isinstance(item, dict):
            raise PolicyError("unsupported public model catalogue schema")
        slug = item.get("slug")
        if slug != CHILD_MODEL:
            continue
        if luna_levels is not None:
            raise PolicyError("duplicate requested model in public catalogue")
        levels = item.get("supported_reasoning_levels")
        if not isinstance(levels, list):
            raise PolicyError("unsupported reasoning-level catalogue")
        efforts = []
        for level in levels:
            if not isinstance(level, dict) or not isinstance(level.get("effort"), str) or not level["effort"]:
                raise PolicyError("unsupported reasoning-level catalogue")
            efforts.append(level["effort"])
        if len(efforts) != len(set(efforts)):
            raise PolicyError("duplicate reasoning level")
        luna_levels = efforts
    if luna_levels is None:
        raise PolicyError("public client catalogue does not contain Luna")
    supported = _canonical_efforts([effort for effort in luna_levels if effort in EFFORTS])
    if not supported:
        raise PolicyError("public client catalogue contains no supported Luna effort")
    if require_full and set(supported) != set(EFFORTS):
        raise PolicyError("public client catalogue does not support all five Luna roles")
    return supported


def direct_supported_efforts(home: Path, codex: str) -> list[str]:
    """Read only Luna's public reasoning-level catalogue from the CLI."""
    if not isinstance(codex, str) or not codex:
        raise PolicyError("official Codex CLI command unavailable")
    result = _run_codex(Path(home), [codex, "debug", "models"],
                        timeout=SOURCE_TIMEOUT, limit=MAX_SOURCE_BYTES)
    return _catalog_efforts(_stdout(result))


# Keep the Go migration's exported spelling available to small adapters and
# tests. A wrapper lets either public spelling be replaced in offline tests.
def DirectSupportedEfforts(home: Path, codex: str) -> list[str]:
    return direct_supported_efforts(home, codex)


def preflight(home: Path, codex: str, *, now: datetime | None = None) -> dict:
    """Read-only public prerequisite check; never writes the snapshot."""
    if not isinstance(codex, str) or not codex:
        raise PolicyError("official Codex CLI command unavailable")
    home = Path(os.path.expanduser(str(home)))
    if not home.is_absolute():
        raise PolicyError("absolute Codex home required")
    cli = _check_cli_version(_stdout(_run_codex(home, [codex, "--version"], timeout=5, limit=65536)))
    efforts = _catalog_efforts(_stdout(_run_codex(home, [codex, "debug", "models"], timeout=5, limit=MAX_SOURCE_BYTES)), require_full=True)
    return {"schema": 2, "checked_at": _stamp(_utc(now)), "supported_efforts": efforts,
            "child_model": CHILD_MODEL, "codex": codex, "cli_version": cli,
            "evidence": "public_client_catalogue"}


def refresh_capabilities(home: Path, codex: str, *, now: datetime | None = None) -> dict:
    """Run the public preflight and atomically store only its public snapshot."""
    home = Path(os.path.expanduser(str(home)))
    if not home.is_absolute():
        raise PolicyError("absolute Codex home required")
    snapshot = preflight(home, codex, now=now)
    _atomic(_snapshot_path(home), _encode(snapshot))
    return snapshot


def doctor_capabilities(home: Path, codex: str) -> dict:
    """Check local CLI version and stored snapshot without catalog/network access."""
    home = Path(os.path.expanduser(str(home)))
    if not home.is_absolute():
        return {"status": "NOT_READY", "cli_version": "", "snapshot": None,
                "errors": ["absolute Codex home required"]}
    result = {"status": "NOT_READY", "cli_version": "", "snapshot": None, "errors": []}
    try:
        result["cli_version"] = _check_cli_version(_stdout(_run_codex(home, [codex, "--version"], timeout=5, limit=65536)))
    except PolicyError as error:
        result["errors"].append(str(error))
    try:
        snapshot = _read_snapshot(_snapshot_path(home), _utc())
        result["snapshot"] = snapshot
    except PolicyError as error:
        result["errors"].append(str(error))
    if not result["errors"]:
        result["status"] = "READY"
    return result


__all__ = [
    "VERSION", "RESOURCE", "EFFORTS", "MODEL_DIAL_URL", "DENG_URL", "PolicyError",
    "config_key", "parse_modeldial", "parse_deng", "fetch_sources", "FetchSources",
    "select_policy", "read_cached", "cached_select", "select", "select_for_root", "read_capabilities",
    "direct_supported_efforts", "DirectSupportedEfforts", "preflight",
    "refresh_capabilities", "doctor_capabilities",
]
