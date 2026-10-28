"""Offline policy tests for native capability and daily-cache boundaries."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_adaptive_agents import policy


UTC = timezone.utc
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def unavailable_sources(*_args, **_kwargs):
    return []


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "codex home"
        self.home.mkdir()
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()
        self.codex = str(Path(self.temp.name).resolve() / "codex")
        self.other_codex = str(Path(self.temp.name).resolve() / "other" / "codex")
        self.now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    def snapshot(self, checked_at, *, codex=None, efforts=None, root_model="gpt-6-astra"):
        codex = codex or str(self.home / 'codex')
        resource = self.home / policy.RESOURCE
        resource.mkdir(exist_ok=True)
        value = {
            "schema": 1,
            "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
            "supported_efforts": efforts or EFFORTS,
            "root_model": root_model,
            "child_model": "gpt-6-luna",
            "codex": codex,
            "cli_version": "codex-cli 0.147.0",
            "evidence": "public_client_catalogue",
        }
        path = resource / "capabilities.json"
        raw = json.dumps(value, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        return path, raw

    def previous_model_cache(self):
        legacy_config = policy._cfg()
        legacy_config.pop("child_model")
        legacy_config["fallback_effort"] = "max"
        state = self.project / ".codex-adaptive-agents" / "state"
        state.mkdir(parents=True, exist_ok=True)
        day_key = "UTC:2026-09-15"
        cache = {
            "schema": 1,
            "attempted_days": {day_key: policy._stamp(self.now)},
            "last_attempt": policy._stamp(self.now),
            "last_success": policy._stamp(self.now),
            "capabilities_at": policy._stamp(self.now),
            "supported_efforts": EFFORTS,
            "timezone": "UTC",
            "day": "2026-09-15",
            "last_status": "fallback",
            "config_key": policy.config_key(legacy_config),
            "policy": {
                "version": "policy-v1-old-model",
                "generated_at": policy._stamp(self.now),
                "default_effort": "max",
                "status": "fallback",
                "reasons": [],
                "sources": [],
                "selected_evidence": [],
                "quality_effort": "",
                "conflict": False,
                "runtime_route": policy.RUNTIME_ROUTE,
            },
        }
        path = state / "policy-cache.json"
        path.write_text(json.dumps(cache), encoding="utf-8")
        return path

    def terra_snapshot(self, *, checked_at=None, efforts=None, cli_version="codex-cli 0.159.0",
                       catalogue_status="available", codex=None):
        resource = self.home / policy.RESOURCE
        resource.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": 1,
            "model": "gpt-6.1-terra",
            "checked_at": policy._stamp(checked_at or self.now),
            "supported_efforts": ["low", "medium", "high", "xhigh"] if efforts is None else efforts,
            "catalogue_status": catalogue_status,
            "codex": codex or self.codex,
            "cli_version": cli_version,
            "evidence": "public_client_catalogue",
        }
        path = resource / "terra-capabilities.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def fake_cli_version(self, value="codex-cli 0.159.0"):
        return patch.object(policy, "_run_codex", return_value=SimpleNamespace(
            returncode=0, stdout=(value + "\n").encode("utf-8")))

    def test_source_json_rejects_duplicates_and_bad_timestamps(self):
        with self.assertRaises(policy.PolicyError):
            policy.parse_modeldial(
                b'{"schemaVersion":"1.1","schemaVersion":"1.1","batch":{},"rankings":[]}'
            )
        with self.assertRaises(policy.PolicyError):
            policy.parse_deng(
                b'{"schema":3,"mode":"equal_latest_3","benchmark_id":"b",'
                b'"source_updated_at":"not-a-time","points":[]}'
            )

    def test_compatibility_selector_ignores_missing_unknown_and_any_root_model(self):
        identities = (None, "unknown", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "future-model")
        with patch.object(policy, "select", return_value={"role": "adaptive_luna_xhigh"}) as selector:
            for root_model in identities:
                with self.subTest(root_model=root_model):
                    self.assertEqual(policy.select_for_root(self.home, self.project, root_model)["role"],
                                 "adaptive_luna_xhigh")
        self.assertEqual(selector.call_count, len(identities))
        self.assertTrue(all(call.args == (self.home, self.project) for call in selector.call_args_list))

    def test_installed_model_policy_file_has_no_root_allowlist(self):
        path = Path(policy.__file__).with_name("model_policy.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(value, {"schema": 1, "delegation_root_policy": "unrestricted"})
        self.assertNotIn("supported_root_models", value)

    def test_child_only_catalogue_and_new_snapshot_need_no_astra_entry(self):
        raw = json.dumps({"models": [{"slug": "gpt-6-luna",
            "supported_reasoning_levels": [{"effort": effort} for effort in EFFORTS]}]}).encode()
        self.assertEqual(policy._catalog_efforts(raw, require_full=True), EFFORTS)

        from types import SimpleNamespace
        def codex_result(_home, argv, **_kwargs):
            output = b"codex-cli 0.155.1" if argv[-1] == "--version" else raw
            return SimpleNamespace(returncode=0, stdout=output)
        with patch.object(policy, "_run_codex", side_effect=codex_result):
            snapshot = policy.preflight(self.home, str(self.home / "codex"), now=self.now)
        self.assertEqual(snapshot["schema"], 2)
        self.assertEqual(snapshot["child_model"], "gpt-6-luna")
        self.assertNotIn("root_model", snapshot)

    def test_legacy_schema_one_snapshot_treats_root_identity_as_provenance(self):
        for root_model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "unknown-future-model"):
            with self.subTest(root_model=root_model):
                self.snapshot(self.now, root_model=root_model)
                snapshot = policy.read_capabilities(self.home, now=self.now)
                self.assertEqual(snapshot["schema"], 2)
                self.assertEqual(snapshot["supported_efforts"], EFFORTS)
                self.assertNotIn("root_model", snapshot)

    def test_same_local_day_reuses_cache_without_refresh(self):
        calls = {"catalog": 0, "sources": 0}

        def catalog(*_args):
            calls["catalog"] += 1
            return list(EFFORTS)

        def sources(*_args, **_kwargs):
            calls["sources"] += 1
            return []

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=catalog), \
                patch.object(policy, "FetchSources", side_effect=sources):
            first = policy.select(self.home, self.project, now=self.now)
            second = policy.select(self.home, self.project,
                                   now=self.now + timedelta(hours=1))

        self.assertEqual(first["role"], "adaptive_luna_xhigh")
        self.assertEqual(second["role"], "adaptive_luna_xhigh")
        self.assertTrue(first["refreshed"])
        self.assertFalse(second["refreshed"])
        self.assertEqual(calls, {"catalog": 1, "sources": 1})

    def test_normal_evidence_cache_is_reusable_offline(self):
        def measurement(source, batch):
            return {
                "source": source, "url": "https://example.invalid/public-fixture",
                "fetched_at": policy._stamp(self.now), "evaluated_at": policy._stamp(self.now),
                "batch": batch, "model": "gpt-6-luna", "effort": "max",
                "category": "backend", "protocol": "fixture-v1", "grader": "fixture-grader",
                "dataset_version": "fixture-pack", "route": policy.RUNTIME_ROUTE,
                "score": 90, "score_max": 100, "duration_ms": None,
                "duration_metric": "", "samples": None, "published_rank": 1,
                "critical_failure": False, "content_hash": "fixture-sha256",
            }

        sources = [
            {"source": name, "status": "ok", "batch": name + "-batch",
             "rows": [measurement(name, name + "-batch")]}
            for name in ("modeldial", "deng")
        ]
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=sources):
            selected = policy.select(self.home, self.project, now=self.now)
            cached = policy.read_cached(self.home, self.project, now=self.now)

        self.assertEqual(selected["status"], "valid")
        self.assertEqual(selected["role"], "adaptive_luna_max")
        self.assertEqual(cached["status"], "valid")
        self.assertEqual(cached["role"], "adaptive_luna_max")

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            preserved = policy.select(self.home, self.project, force=True,
                                       now=self.now + timedelta(hours=1))
        self.assertEqual(preserved["status"], "degraded")
        self.assertEqual(preserved["role"], "adaptive_luna_max")

    def test_explicit_force_repairs_invalid_cache_but_normal_select_preserves_it(self):
        cache_path = self.project / ".codex-adaptive-agents/state/policy-cache.json"
        cache_path.parent.mkdir(parents=True)
        malformed = b'{"schema":true,"policy":{}}\n'
        cache_path.write_bytes(malformed)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                             side_effect=AssertionError("normal select must not repair invalid cache")), \
                patch.object(policy, "FetchSources",
                             side_effect=AssertionError("normal select must not fetch")):
            blocked = policy.select(self.home, self.project, now=self.now)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["role"], "")
        self.assertEqual(cache_path.read_bytes(), malformed)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            repaired = policy.select(self.home, self.project, force=True, now=self.now)
        self.assertEqual(repaired["status"], "fallback")
        self.assertEqual(repaired["role"], "adaptive_luna_xhigh")
        restored = policy._cache(cache_path)
        self.assertEqual(restored["schema"], 1)
        self.assertEqual(restored["attempted_days"]["UTC:2026-09-15"], policy._stamp(self.now))

    def test_strict_json_normalizes_python_oversized_integer_error(self):
        digit_limit = sys.get_int_max_str_digits()
        if digit_limit == 0:
            self.skipTest("Python integer conversion limit is disabled")
        digits = max(5000, digit_limit + 1)
        raw = (b'{"schema":1,"attempted_days":{"UTC:2026-09-15":' +
               b"9" * digits + b"}}\n")
        self.assertLess(len(raw), policy.MAX_CACHE_BYTES)
        with self.assertRaisesRegex(policy.PolicyError, "invalid JSON"):
            policy._strict_json(raw)
        self.assertEqual(sys.get_int_max_str_digits(), digit_limit)

    def test_oversized_integer_cache_blocks_unchanged_and_force_repairs_under_lock(self):
        digit_limit = sys.get_int_max_str_digits()
        if digit_limit == 0:
            self.skipTest("Python integer conversion limit is disabled")
        digits = max(5000, digit_limit + 1)
        malformed = (b'{"schema":1,"attempted_days":{"UTC:2026-09-15":' +
                     b"9" * digits + b"}}\n")
        self.assertLess(len(malformed), policy.MAX_CACHE_BYTES)
        cache_path = self.project / ".codex-adaptive-agents/state/policy-cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_bytes(malformed)

        entered_locks = []
        real_file_lock = policy._platform.file_lock

        @contextmanager
        def tracked_file_lock(path, timeout=5):
            with real_file_lock(path, timeout=timeout):
                entered_locks.append(Path(path))
                yield

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                             side_effect=AssertionError("blocked select must not probe")), \
                patch.object(policy, "FetchSources",
                             side_effect=AssertionError("blocked select must not fetch")):
            blocked = policy.select(self.home, self.project, now=self.now)
            cached = policy.read_cached(self.home, self.project, now=self.now)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(cached["status"], "blocked")
        self.assertEqual(cache_path.read_bytes(), malformed)
        self.assertEqual(entered_locks, [])

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy._platform, "file_lock", side_effect=tracked_file_lock), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            repaired = policy.select(self.home, self.project, force=True, now=self.now)
        self.assertEqual(repaired["status"], "fallback")
        self.assertEqual(repaired["role"], "adaptive_luna_xhigh")
        self.assertEqual(entered_locks, [self.project / ".codex-adaptive-agents/state/policy-refresh.lock"])
        self.assertNotEqual(cache_path.read_bytes(), malformed)
        restored = policy._cache(cache_path)
        self.assertEqual(restored["schema"], 1)
        self.assertEqual(restored["attempted_days"]["UTC:2026-09-15"], policy._stamp(self.now))

    def test_force_refresh_does_not_rehash_bad_previous_version_or_sources(self):
        def measurement(source, batch):
            return {
                "source": source, "url": "https://example.invalid/previous-fixture",
                "fetched_at": policy._stamp(self.now), "evaluated_at": policy._stamp(self.now),
                "batch": batch, "model": "gpt-6-luna", "effort": "max",
                "category": "backend", "protocol": "fixture-v1", "grader": "fixture-grader",
                "dataset_version": "fixture-pack", "route": policy.RUNTIME_ROUTE,
                "score": 90, "score_max": 100, "duration_ms": None,
                "duration_metric": "", "samples": None, "published_rank": 1,
                "critical_failure": False, "content_hash": "fixture-sha256",
            }

        sources = [
            {"source": name, "status": "ok", "batch": name + "-batch",
             "rows": [measurement(name, name + "-batch")]}
            for name in ("modeldial", "deng")
        ]
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=sources):
            initial = policy.select(self.home, self.project, now=self.now)
        self.assertEqual(initial["status"], "valid")
        self.assertEqual(initial["role"], "adaptive_luna_max")

        cache_path = self.project / ".codex-adaptive-agents/state/policy-cache.json"
        pristine = json.loads(cache_path.read_text(encoding="utf-8"))
        attacks = {
            "bad version": lambda value: value["policy"].update(version="policy-v1-bad"),
            "sources no longer bind evidence": lambda value: value["policy"].update(sources=[]),
        }
        for label, mutate in attacks.items():
            with self.subTest(attack=label):
                value = json.loads(json.dumps(pristine))
                mutate(value)
                if label == "sources no longer bind evidence":
                    decision_config = dict(policy._cfg())
                    decision_config["supported_efforts"] = list(value["supported_efforts"])
                    value["policy"]["version"] = policy._decision_version(
                        decision_config, value["policy"])
                cache_path.write_text(json.dumps(value), encoding="utf-8")
                with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                        patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                        patch.object(policy, "FetchSources", return_value=[]):
                    result = policy.select(self.home, self.project, force=True,
                                           now=self.now + timedelta(hours=1))
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(result["role"], "adaptive_luna_xhigh")
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["policy"]["selected_evidence"], [])

    def test_cache_rejects_boolean_schema_and_inconsistent_decisions(self):
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            produced = policy.select(self.home, self.project, now=self.now)
            self.assertEqual(produced["role"], "adaptive_luna_xhigh")
            self.assertEqual(produced["status"], "fallback")
            self.assertEqual(policy.read_cached(self.home, self.project, now=self.now)["role"],
                             "adaptive_luna_xhigh")

        cache_path = self.project / ".codex-adaptive-agents/state/policy-cache.json"
        original = json.loads(cache_path.read_text(encoding="utf-8"))
        cases = {
            "boolean schema": lambda value: value.update(schema=True),
            "unbacked valid status": lambda value: value["policy"].update(status="valid"),
            "wrong route": lambda value: value["policy"].update(runtime_route="other-route"),
            "wrong version": lambda value: value["policy"].update(version="policy-v1-" + "0" * 24),
            "malformed timestamp": lambda value: value.update(capabilities_at="not-a-time"),
            "wrong reasons shape": lambda value: value["policy"].update(reasons=123),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                value = json.loads(json.dumps(original))
                mutate(value)
                cache_path.write_text(json.dumps(value), encoding="utf-8")
                result = policy.read_cached(self.home, self.project, now=self.now)
                self.assertEqual(result["role"], "")
                self.assertEqual(result["effort"], "")
                self.assertEqual(result["status"], "blocked")
        cache_path.write_text(json.dumps(original), encoding="utf-8")

        boolean_schema = json.loads(json.dumps(original))
        boolean_schema["schema"] = True
        cache_path.write_text(json.dumps(boolean_schema), encoding="utf-8")
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                             side_effect=AssertionError("invalid cache must not reset daily budget")), \
                patch.object(policy, "FetchSources",
                             side_effect=AssertionError("invalid cache must not fetch")):
            selected = policy.select(self.home, self.project, now=self.now)
        self.assertEqual(selected["role"], "")
        self.assertEqual(selected["effort"], "")
        self.assertEqual(selected["status"], "blocked")

        broken_time = json.loads(json.dumps(original))
        broken_time["capabilities_at"] = "not-a-time"
        cache_path.write_text(json.dumps(broken_time), encoding="utf-8")
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                             side_effect=AssertionError("same-day unusable cache must not refresh")), \
                patch.object(policy, "FetchSources",
                             side_effect=AssertionError("same-day unusable cache must not fetch")):
            selected = policy.select(self.home, self.project, now=self.now)
        self.assertEqual(selected["role"], "")
        self.assertEqual(selected["effort"], "")
        self.assertEqual(selected["status"], "blocked")

    def test_snapshot_cli_path_is_not_passed_to_selector_as_execution_authority(self):
        marker = self.home / "should-not-run"
        fake = self.home / "codex-from-snapshot"
        fake.write_text("#!/bin/sh\ntouch " + str(marker) + "\n", encoding="utf-8")
        fake.chmod(0o755)
        self.snapshot(self.now, codex=str(fake))
        observed = []

        def catalog(_home, codex):
            observed.append(codex)
            return list(EFFORTS)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=catalog), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertEqual(observed, ["codex"])
        self.assertFalse(marker.exists())

    def test_system_timezone_controls_natural_day(self):
        with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}, clear=False), \
                patch.object(policy, "ZoneInfo", return_value=timezone(timedelta(hours=8))), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(
                self.home, self.project,
                now=datetime(2026, 9, 15, 16, 0, tzinfo=UTC),
            )

        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["day"], "2026-09-16")

    def test_fresh_snapshot_fallback_keeps_original_checked_at_and_stays_stale(self):
        checked_at = self.now - timedelta(hours=2)
        path, original = self.snapshot(checked_at)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                              side_effect=policy.PolicyError("catalog unavailable")), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        cache = json.loads((self.project / ".codex-adaptive-agents/state/policy-cache.json").read_text())
        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertEqual(result["status"], "fallback")
        self.assertTrue(result["stale"])
        self.assertEqual(cache["capabilities_at"], policy._stamp(checked_at))
        self.assertEqual(path.read_bytes(), original)

    def test_expired_snapshot_still_probes_direct_catalogue(self):
        path, original = self.snapshot(self.now - timedelta(hours=73))
        seen = []

        def catalog(home, codex):
            seen.append((home, codex))
            return list(EFFORTS)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=catalog), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertEqual(seen, [(self.home, "codex")])
        self.assertEqual(result["capabilities_at"], policy._stamp(self.now))
        self.assertEqual(path.read_bytes(), original)

    def test_expired_snapshot_is_not_used_when_probe_times_out(self):
        path, original = self.snapshot(self.now - timedelta(hours=73))

        def slow_catalogue(*_args):
            time.sleep(0.1)
            return list(EFFORTS)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "SOURCE_TIMEOUT", 0.02), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=slow_catalogue), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["capabilities_at"], policy._stamp(None))
        self.assertEqual(path.read_bytes(), original)

    def test_future_snapshot_is_not_used_when_probe_times_out(self):
        path, original = self.snapshot(self.now + timedelta(hours=1))

        def slow_catalogue(*_args):
            time.sleep(0.1)
            return list(EFFORTS)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "SOURCE_TIMEOUT", 0.02), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=slow_catalogue), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["capabilities_at"], policy._stamp(None))
        self.assertEqual(path.read_bytes(), original)

    def test_timeout_uses_fresh_snapshot_without_extending_checked_at(self):
        checked_at = self.now - timedelta(hours=1)
        path, original = self.snapshot(checked_at)

        def slow_catalogue(*_args):
            time.sleep(0.1)
            return list(EFFORTS)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "SOURCE_TIMEOUT", 0.02), \
                patch.object(policy, "DirectSupportedEfforts", side_effect=slow_catalogue), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["capabilities_at"], policy._stamp(checked_at))
        self.assertEqual(path.read_bytes(), original)

    def test_successful_probe_takes_precedence_over_fresh_snapshot(self):
        checked_at = self.now - timedelta(hours=1)
        self.snapshot(checked_at)

        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=list(EFFORTS)), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        cache = json.loads((self.project / ".codex-adaptive-agents/state/policy-cache.json").read_text())
        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertEqual(cache["capabilities_at"], policy._stamp(self.now))
        self.assertNotIn("used fresh public capability snapshot", " ".join(result["reasons"]))

    def test_missing_capability_blocks_and_does_not_invent_success(self):
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                              side_effect=policy.PolicyError("catalog unavailable")), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["role"], "")
        self.assertEqual(result["effort"], "")
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["stale"])
        cache = json.loads((self.project / ".codex-adaptive-agents/state/policy-cache.json").read_text())
        self.assertEqual(cache["last_success"], "0001-01-01T00:00:00Z")

    def test_unusable_radar_scores_fall_back_to_xhigh_even_when_max_is_supported(self):
        modeldial = {
            "schemaVersion": "1.1",
            "batch": {
                "id": "public-batch",
                "publishedAt": "2026-09-15T11:00:00Z",
                "evaluationProfile": "profile-v1",
                "graderVersion": "grader-v1",
                "questionPackVersion": "pack-v1",
            },
            "rankings": [{
                "model": "gpt-6-luna", "reasoningEffort": "max", "score": None,
                "maxScore": 100, "scoreBasis": "backend", "route": "codex_official_login",
            }],
        }
        deng = {
            "schema": 3,
            "mode": "equal_latest_3",
            "benchmark_id": "cohort-v1",
            "source_updated_at": "2026-09-15T11:00:00Z",
            "scoring_mode": "iq-v1",
            "points": [{
                "model": "gpt-6-luna", "effort": "max", "iq": None,
                "total": 20, "average_minutes": 10,
                "source_updated_at": "2026-09-15T11:00:00Z",
            }],
        }
        sources = [
            policy.parse_modeldial(json.dumps(modeldial).encode(), self.now),
            policy.parse_deng(json.dumps(deng).encode(), self.now),
        ]

        result = policy.select_policy(self.now, sources)

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["default_effort"], "xhigh")

    def test_older_model_radar_rows_are_ignored(self):
        modeldial = {
            "schemaVersion": "1.1",
            "batch": {"id": "old-batch", "publishedAt": "2026-09-15T11:00:00Z"},
            "rankings": [{"model": "gpt-5.6-luna", "reasoningEffort": "max", "score": 100,
                          "maxScore": 100}],
        }
        deng = {
            "schema": 3, "mode": "equal_latest_3", "benchmark_id": "old-cohort",
            "source_updated_at": "2026-09-15T11:00:00Z",
            "points": [{"model": "gpt-5.6-luna", "effort": "max", "iq": 150,
                        "source_updated_at": "2026-09-15T11:00:00Z"}],
        }

        self.assertEqual(policy.parse_modeldial(json.dumps(modeldial).encode(), self.now)["rows"], [])
        self.assertEqual(policy.parse_deng(json.dumps(deng).encode(), self.now)["rows"], [])

    def test_unavailable_configured_fallback_blocks_instead_of_upgrading_to_max(self):
        available = ["low", "medium", "high", "max"]
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=available), \
                patch.object(policy, "FetchSources", return_value=[]):
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["role"], "")
        self.assertIn("xhigh", " ".join(result["reasons"]))

    def test_previous_model_daily_cache_refreshes_once_for_new_identity(self):
        cache_path = self.previous_model_cache()
        available = ["low", "xhigh", "max"]
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts", return_value=available) as catalog, \
                patch.object(policy, "FetchSources", return_value=[]) as sources:
            result = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["role"], "adaptive_luna_xhigh")
        self.assertTrue(result["refreshed"])
        catalog.assert_called_once()
        sources.assert_called_once()
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cache["config_key"], policy.config_key())
        self.assertEqual(cache["supported_efforts"], available)
        self.assertEqual(cache["policy"]["selected_evidence"], [])

    def test_config_key_changes_with_child_model_identity(self):
        previous_model_config = policy._cfg()
        previous_model_config.pop("child_model")

        self.assertNotEqual(policy.config_key(previous_model_config), policy.config_key())

    def test_previous_model_capabilities_are_not_reused_when_new_probe_fails(self):
        cache_path = self.previous_model_cache()
        with patch.dict(os.environ, {"TZ": "UTC"}, clear=False), \
                patch.object(policy, "DirectSupportedEfforts",
                             side_effect=policy.PolicyError("new model catalog unavailable")) as catalog, \
                patch.object(policy, "FetchSources", return_value=[]):
            first = policy.select(self.home, self.project, now=self.now)
            second = policy.select(self.home, self.project, now=self.now)

        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["role"], "")
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["role"], "")
        catalog.assert_called_once()
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cache["config_key"], policy.config_key())
        self.assertEqual(cache["supported_efforts"], [])
        self.assertEqual(cache["capabilities_at"], policy._stamp(None))

    def test_old_capability_snapshot_without_child_model_identity_is_rejected(self):
        path, _original = self.snapshot(self.now)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot.pop("child_model")
        path.write_text(json.dumps(snapshot), encoding="utf-8")

        with self.assertRaisesRegex(policy.PolicyError, "capability snapshot unavailable"):
            policy.read_capabilities(self.home, now=self.now)

    def test_rating_evidence_can_still_choose_a_faster_near_quality_effort(self):
        def measurement(effort, score, duration, rank):
            return {
                "source": "modeldial", "url": policy.MODEL_DIAL_URL,
                "fetched_at": policy._stamp(self.now), "evaluated_at": policy._stamp(self.now),
                "batch": "rated-batch", "model": "gpt-6-luna", "effort": effort,
                "category": "backend", "protocol": "profile-v1", "grader": "grader-v1",
                "dataset_version": "pack-v1", "route": policy.RUNTIME_ROUTE,
                "score": score, "score_max": 100, "duration_ms": duration,
                "duration_metric": "runtime_ms", "samples": 5, "published_rank": rank,
                "critical_failure": False, "content_hash": "sha256:public-sample",
            }

        source = {
            "source": "modeldial", "status": "ok", "batch": "rated-batch",
            "rows": [measurement("max", 100, 1000, 1),
                     measurement("high", 99.5, 600, 2)],
        }

        result = policy.select_policy(self.now, [source])

        self.assertEqual(result["quality_effort"], "max")
        self.assertEqual(result["default_effort"], "high")
        self.assertEqual(result["status"], "degraded")

    def test_no_iana_database_uses_system_local_date_and_portable_utc(self):
        with patch.dict(os.environ, {"TZ": ""}), \
                patch.object(policy, "ZoneInfo", side_effect=KeyError("database absent")):
            name, zone, day = policy._system_timezone(self.now)
            self.assertTrue(name.startswith('local:'))
            self.assertEqual(day, self.now.astimezone().date().isoformat())
            self.assertEqual(self.now.astimezone(zone).date().isoformat(), day)
        with patch.dict(os.environ, {"TZ": "UTC"}), \
                patch.object(policy, "ZoneInfo", side_effect=KeyError("database absent")):
            self.assertEqual(policy._system_timezone(self.now), ('UTC', timezone.utc, '2026-09-15'))
        with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}), \
                patch.object(policy, "ZoneInfo", side_effect=KeyError("database absent")):
            with self.assertRaisesRegex(policy.PolicyError, 'unset TZ'):
                policy._system_timezone(self.now)

    def test_computer_and_browser_route_only_to_exact_fresh_terra_capability(self):
        self.terra_snapshot()
        for task_kind in ("computer", "browser"):
            with self.subTest(task_kind=task_kind), self.fake_cli_version(), \
                    patch.object(policy, "DirectSupportedEfforts",
                                 side_effect=AssertionError("Terra route must not read Luna cache")), \
                    patch.object(policy, "FetchSources",
                                 side_effect=AssertionError("Terra route must not score Luna")):
                selected = policy.select_task_kind(self.home, self.project, task_kind,
                                                   codex=self.codex, now=self.now)
            self.assertEqual((selected["role"], selected["model"], selected["effort"]),
                             ("adaptive_terra_xhigh", "gpt-6.1-terra", "xhigh"))
            self.assertEqual(selected["task_kind"], task_kind)
            self.assertEqual(selected["capability_source"], "public_client_catalogue")

    def test_old_luna_snapshot_and_model_provenance_never_prove_terra(self):
        path, _ = self.snapshot(self.now, root_model="gpt-6.1-terra")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["cli_version"] = "codex-cli 0.159.0"
        value["codex"] = "/path/with/terra/in/name"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.fake_cli_version():
            selected = policy.select_task_kind(self.home, self.project, "computer",
                                               codex=self.codex, now=self.now)
        self.assertEqual(selected["role"], "")
        self.assertIn("Terra capability snapshot unavailable; run refresh", selected["reasons"])
        self.assertFalse((self.home / policy.RESOURCE / "terra-capabilities.json").exists())

    def test_terra_route_blocks_stale_unsupported_conflicting_or_cli_mismatched_capabilities(self):
        cases = (
            ("stale", dict(checked_at=self.now - timedelta(hours=73)), "expired"),
            ("xhigh unsupported", dict(efforts=["low", "medium", "high"]), "does not support gpt-6.1-terra xhigh"),
            ("duplicate declaration", dict(efforts=[], catalogue_status="conflict"), "conflicting gpt-6.1-terra"),
            ("snapshot old CLI", dict(cli_version="codex-cli 0.158.0"), "snapshot was recorded by Codex CLI older"),
            ("snapshot version mismatch", dict(cli_version="codex-cli 0.160.0"), "differs from the current CLI"),
            ("snapshot source path mismatch", dict(codex=self.other_codex), "path differs from the current CLI"),
        )
        for label, snapshot_args, expected in cases:
            with self.subTest(label=label):
                self.terra_snapshot(**snapshot_args)
                with self.fake_cli_version():
                    selected = policy.select_task_kind(self.home, self.project, "browser",
                                                       codex=self.codex, now=self.now)
                self.assertEqual(selected["role"], "")
                self.assertIn(expected, selected["reasons"][0])
                (self.home / policy.RESOURCE / "terra-capabilities.json").unlink()

        self.terra_snapshot()
        with self.fake_cli_version("codex-cli 0.158.0"):
            old_cli = policy.select_task_kind(self.home, self.project, "computer",
                                              codex=self.codex, now=self.now)
        self.assertEqual(old_cli["role"], "")
        self.assertIn("requires Codex CLI 0.159.0 or later", old_cli["reasons"][0])

    def test_catalogue_preflight_binds_separate_terra_sidecar_and_luna_remains_usable(self):
        catalog = {
            "models": [
                {"slug": "gpt-6-luna", "supported_reasoning_levels": [
                    {"effort": effort} for effort in EFFORTS]},
                {"slug": "gpt-6.1-terra", "supported_reasoning_levels": [
                    {"effort": effort} for effort in ["low", "medium", "high", "xhigh"]]},
                {"slug": "gpt-6.1-terra", "supported_reasoning_levels": [
                    {"effort": effort} for effort in ["low", "medium", "high", "max"]]},
            ]
        }
        calls = []

        def fake_run(_home, argv, **_kwargs):
            calls.append(argv)
            raw = (b"codex-cli 0.159.0\n" if argv[-1] == "--version" else
                   json.dumps(catalog).encode("utf-8"))
            return SimpleNamespace(returncode=0, stdout=raw)

        with patch.object(policy, "_run_codex", side_effect=fake_run):
            snapshots = policy.preflight_all(self.home, self.codex, now=self.now)
        self.assertEqual([call[1:] for call in calls], [["--version"], ["debug", "models"]])
        self.assertEqual(snapshots["luna"]["schema"], 2)
        self.assertEqual(snapshots["luna"]["supported_efforts"], EFFORTS)
        self.assertEqual(snapshots["terra"]["model"], "gpt-6.1-terra")
        self.assertEqual(snapshots["terra"]["supported_efforts"], [])
        self.assertEqual(snapshots["terra"]["catalogue_status"], "conflict")
        self.assertEqual(snapshots["terra"]["checked_at"], policy._stamp(self.now))
        self.assertEqual(snapshots["terra"]["cli_version"], "codex-cli 0.159.0")
        self.assertEqual(snapshots["terra"]["evidence"], "public_client_catalogue")


if __name__ == "__main__":
    unittest.main()
