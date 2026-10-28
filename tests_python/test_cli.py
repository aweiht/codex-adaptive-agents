"""Real isolated Python entry lifecycle with fake public CLI/source data."""
from datetime import datetime, timezone
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_adaptive_agents import cli, install, policy, process_registry, verify
from codex_adaptive_agents.platform import clean_env, run


class CLITests(unittest.TestCase):
    def test_doctor_ignores_snapshot_executable_and_resolves_normal_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'home'
            resource = home / install.RESOURCE
            resource.mkdir(parents=True)
            marker = Path(directory).resolve() / 'snapshot-executable-ran'
            fake = Path(directory).resolve() / 'snapshot-codex'
            fake.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n', encoding='utf-8')
            fake.chmod(0o755)
            (resource / 'capabilities.json').write_text(
                json.dumps({'schema': 2, 'codex': str(fake)}), encoding='utf-8')
            with patch.object(install, 'validate', return_value={'schema': 4, 'version': install.__version__}), \
                    patch.object(install, 'build'), \
                    patch.object(cli, 'read', return_value=b''), \
                    patch.object(install, 'desired', return_value={}), \
                    patch.object(install, 'transform', return_value=(b'', [])), \
                    patch.object(cli.shutil, 'which', return_value=os.path.abspath('/normal/PATH/codex')) as which, \
                    patch.object(policy, 'doctor_capabilities', return_value={'status': 'READY'}) as capabilities, \
                    patch.object(policy, 'cached_select', return_value={'role': 'adaptive_luna_xhigh',
                          'status': 'fallback'}), \
                    patch.object(cli, 'run', return_value=SimpleNamespace(
                        returncode=0, stdout=(install.__version__ + '\n').encode())):
                result = cli.doctor(home)

        self.assertEqual(result['status'], 'READY_FALLBACK')
        which.assert_called_once_with('codex')
        capabilities.assert_called_once_with(home, os.path.abspath('/normal/PATH/codex'))
        self.assertFalse(marker.exists())

    def test_resolve_codex_preserves_explicit_path_and_uses_path_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'home'
            resource = home / install.RESOURCE
            resource.mkdir(parents=True)
            (resource / 'capabilities.json').write_text(
                json.dumps({'schema': 2, 'codex': '/untrusted/from/snapshot'}), encoding='utf-8')
            path_result = os.path.abspath('/normal/PATH/codex')
            explicit_result = os.path.abspath('/known/explicit/codex')
            with patch.object(cli.shutil, 'which', side_effect=lambda value: {
                    'codex': path_result, '/known/explicit/codex': explicit_result
            }.get(value)) as which:
                self.assertEqual(cli.resolve_codex(home), path_result)
                self.assertEqual(cli.resolve_codex(home, '/known/explicit/codex'),
                                 explicit_result)
        self.assertEqual([call.args[0] for call in which.call_args_list],
                         ['codex', '/known/explicit/codex'])

    def test_invalid_safety_flags_reject_before_side_effects(self):
        cases = [
            (['verify', '--live', '--dry-run'], '--dry-run'),
            (['refresh', '--dry-run'], '--dry-run'),
            (['select', '--dry-run'], '--dry-run'),
            (['doctor', '--dry-run'], '--dry-run'),
            (['recover', '--yes', '--dry-run'], '--dry-run'),
            (['rollback', '--backup', 'recorded', '--yes', '--dry-run'], '--dry-run'),
            (['process-init', '--project', '.', '--dry-run'], '--dry-run'),
            (['process-init', '--project', '.', '--summary'], '--summary'),
            (['process-check', '--project', '.', '--run-id', 'a' * 32, '--summary'], '--summary'),
            (['doctor', '--summary'], 'process options'),
            (['refresh', '--root-model', 'gpt-6.1-sol'], 'only valid for select or verify'),
            (['select', '--task-kind', 'desktop'], 'task-kind must be'),
            (['select', '--task-kind', ''], 'task-kind must be'),
            (['doctor', '--task-kind', 'general'], 'task-kind is only valid for select'),
            (['refresh', '--task-kind', 'browser'], 'task-kind is only valid for select'),
            (['doctor', '--role', 'adaptive_terra_xhigh'], '--role is only valid for verify'),
            (['select', '--root-effort', 'high'], 'only valid for verify'),
            (['internal-smoke-check', '--project', '.', '--dry-run'], '--dry-run'),
            (['recover', '--backup', 'recorded', '--yes'], '--backup'),
            (['install', '--backup', 'recorded', '--yes'], '--backup'),
            (['rollback', '--yes'], '--backup'),
            (['rollback', '--backup', '', '--yes'], '--backup'),
        ]
        boundaries = [(install, 'validate'), (install, 'build'), (install, 'apply'),
                      (install, 'recover'), (cli, 'resolve_codex'), (cli, 'doctor'),
                      (policy, 'select'), (policy, 'refresh_capabilities'),
                      (policy, 'select_task_kind'),
                      (verify, 'run'), (verify, 'check'),
                      (process_registry, 'init_run'), (process_registry, 'check_run'),
                      (process_registry, 'cleanup_run'), (tempfile, 'mkdtemp')]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'must not be created'
            for arguments, expected in cases:
                with self.subTest(arguments=arguments), ExitStack() as stack:
                    mocks = [stack.enter_context(patch.object(module, name))
                             for module, name in boundaries]
                    output = stack.enter_context(redirect_stdout(StringIO()))
                    rc = cli.main(['--codex-home', str(home), *arguments])
                    result = json.loads(output.getvalue())
                    self.assertEqual(rc, 1)
                    self.assertEqual(result['status'], 'ERROR')
                    self.assertIn(expected, result['error'])
                    for mocked in mocks:
                        mocked.assert_not_called()
                    self.assertFalse(home.exists())

    def test_select_uses_the_same_child_policy_for_missing_unknown_and_any_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'home'
            identities = (None, 'unknown', 'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna', 'future-model')
            for identity in identities:
                arguments = ['select', '--explain']
                if identity is not None:
                    arguments += ['--root-model', identity]
                with self.subTest(identity=identity), patch.object(install, 'validate', return_value={'schema': 4}), \
                        patch.object(policy, 'select', return_value={'status': 'fallback',
                            'role': 'adaptive_luna_xhigh', 'effort': 'xhigh'}) as selector, \
                        redirect_stdout(StringIO()) as output:
                    rc = cli.main(['--codex-home', str(home), *arguments])
                    result = json.loads(output.getvalue())
                self.assertEqual(result['role'], 'adaptive_luna_xhigh')
                self.assertEqual(rc, 0)
                selector.assert_called_once()
            self.assertFalse(home.exists())

    def test_recovery_and_explicit_rollback_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / 'home'
            for command, backup in [('recover', None), ('rollback', 'recorded-backup')]:
                with self.subTest(command=command), \
                        patch.object(install, 'recover', return_value={'status': 'ROLLBACK_EXACT_PASS'}) as recover, \
                        redirect_stdout(StringIO()) as output:
                    arguments = ['--codex-home', str(home), command, '--yes']
                    if backup:
                        arguments += ['--backup', backup]
                    self.assertEqual(cli.main(arguments), 0)
                    self.assertEqual(json.loads(output.getvalue())['status'], 'ROLLBACK_EXACT_PASS')
                    recover.assert_called_once_with(home, backup)

    def test_portable_moved_download_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / 'download space 中文'
            for name in install.RUNTIME_FILES:
                dest = source / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(install.ROOT / name, dest)
            home = base / 'codex home 中文'
            home.mkdir()
            (home / 'config.toml').write_text('# keep\nnotify=["x"]\n', encoding='utf-8')
            fakepy = base / 'fake_codex.py'
            fakepy.write_text('''import json, sys
args=sys.argv[1:]
if args == ['--version']: print('codex-cli 0.159.0')
elif args == ['debug','models']:
 print(json.dumps({'models': [
  {'slug':'gpt-6-astra','supported_reasoning_levels':[{'effort':'max'}]},
  {'slug':'gpt-6-luna','supported_reasoning_levels':[{'effort':e} for e in ('low','medium','high','xhigh','max')]},
  {'slug':'gpt-6.1-terra','supported_reasoning_levels':[{'effort':e} for e in ('low','medium','high','xhigh')]}
 ]}))
else: sys.exit(2)
''', encoding='utf-8')
            launcher = base / ('codex.cmd' if os.name == 'nt' else 'codex')
            if os.name == 'nt':
                launcher.write_text('@echo off\r\n"' + sys.executable + '" "' + str(fakepy) + '" %*\r\n', encoding='utf-8')
            else:
                launcher.write_text('#!' + sys.executable + '\n' + fakepy.read_text(encoding='utf-8'), encoding='utf-8')
                launcher.chmod(0o755)
            # Only external data is mocked. Execute the actual entry file and
            # moved installed module tree in separate Python processes.
            runner = base / 'offline_runner.py'
            runner.write_text('''import runpy, sys
from pathlib import Path
entry=sys.argv.pop(1)
sys.path.insert(0,str(Path(entry).parent))
from codex_adaptive_agents import policy
policy.fetch_sources=lambda *a,**k: []
policy.FetchSources=policy.fetch_sources
sys.argv[0]=entry
runpy.run_path(entry,run_name='__main__')
''', encoding='utf-8')
            env = clean_env()
            # No Go in PATH. Python and the fake Codex are resolved absolutely.
            env['PATH'] = os.environ.get('SystemRoot', 'C:\\Windows') + '\\System32' if os.name == 'nt' else '/usr/bin:/bin'
            probe = run([str(launcher), '--version'], env=env, timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(probe.stdout.strip(), b'codex-cli 0.159.0')
            def invoke(entry, *args, expect=0):
                result = run([sys.executable, '-B', str(runner), str(entry), '--codex-home', str(home),
                              '--codex', str(launcher), *args], env=env, timeout=30)
                self.assertEqual(result.returncode, expect, result.stdout.decode('utf-8') + result.stderr.decode('utf-8'))
                return json.loads(result.stdout)
            entry = source / 'codex-adaptive-agents.py'
            initial = invoke(entry, 'doctor', expect=1)
            self.assertEqual(initial['status'], 'NOT_READY')
            plan = invoke(entry, 'install', '--dry-run')
            self.assertTrue(plan['plan_id'])
            self.assertFalse((home / install.ENTRY).exists())
            installed = invoke(entry, 'install', '--yes')
            self.assertEqual(installed['status'], 'INSTALLED')
            source.rename(base / 'download moved')
            entry = home / install.ENTRY
            ready = invoke(entry, 'doctor')
            self.assertEqual(ready['status'], 'READY_FALLBACK')
            project = base / 'project space'
            project.mkdir()
            # The installed entry retains process handoff after the download
            # moves; worker argv after -- is never parsed as installer flags.
            handoff = invoke(entry, 'process-init', '--project', str(project))
            run_id = handoff['run_id']
            command = invoke(entry, 'process-run', '--project', str(project),
                             '--run-id', run_id, '--owner', 'cli-test',
                             '--purpose', 'installed handoff verification', '--',
                             sys.executable, '-c',
                             'import sys; assert sys.argv[1:] == ["--yes", "--yes"]; print("handoff-ok")',
                             '--yes', '--yes')
            self.assertEqual(command['status'], 'SUCCESS')
            summary_command = invoke(entry, 'process-run', '--project', str(project),
                                    '--run-id', run_id, '--owner', 'cli-summary-test',
                                    '--purpose', 'installed compact output', '--summary', '--',
                                    sys.executable, '-c',
                                    'import sys; sys.stdout.write("verbose-" * 4000)')
            self.assertEqual(summary_command['status'], 'SUCCESS')
            self.assertNotIn('stdout', summary_command)
            self.assertNotIn('stderr', summary_command)
            self.assertLess(len(json.dumps(summary_command).encode('utf-8')), 4096)
            self.assertTrue(summary_command['output_complete'])
            self.assertEqual(summary_command['captured_bytes']['stdout'], len('verbose-') * 4000)
            self.assertEqual(Path(summary_command['logs']['stdout']).read_text(), 'verbose-' * 4000)
            checked = invoke(entry, 'process-check', '--project', str(project), '--run-id', run_id)
            self.assertEqual(checked['status'], 'CLEAN')
            self.assertEqual(invoke(entry, 'process-cleanup', '--project', str(project),
                                    '--run-id', run_id)['status'], 'CLEAN')
            rejected = invoke(entry, 'process-run', '--project', str(project), '--run-id', run_id,
                              '--owner', 'late', '--purpose', 'must reject a closed run', '--',
                              sys.executable, '-c', 'raise RuntimeError("must not execute")', expect=1)
            self.assertEqual(rejected['status'], 'ERROR')
            selected = invoke(entry, 'select', '--project', str(project), '--explain')
            self.assertEqual(selected['role'], 'adaptive_luna_xhigh')
            self.assertFalse(invoke(entry, 'select', '--project', str(project), '--explain')['refreshed'])
            for task_kind in ('computer', 'browser'):
                routed = invoke(entry, 'select', '--project', str(project),
                                '--task-kind', task_kind, '--explain')
                self.assertEqual((routed['role'], routed['model'], routed['effort']),
                                 ('adaptive_terra_xhigh', 'gpt-6.1-terra', 'xhigh'))
                self.assertEqual(routed['task_kind'], task_kind)
            terra_sidecar = home / policy.RESOURCE / 'terra-capabilities.json'
            saved_terra_sidecar = terra_sidecar.read_bytes()
            terra_sidecar.unlink()
            blocked = invoke(entry, 'select', '--project', str(project),
                             '--task-kind', 'browser', '--explain', expect=1)
            self.assertEqual(blocked['status'], 'blocked')
            self.assertEqual(blocked['role'], '')
            self.assertIn('Terra capability snapshot unavailable', blocked['reasons'][0])
            self.assertEqual(invoke(entry, 'select', '--project', str(project), '--explain')['role'],
                             'adaptive_luna_xhigh')
            terra_sidecar.write_bytes(saved_terra_sidecar)
            self.assertEqual(invoke(entry, 'refresh')['role'], 'adaptive_luna_xhigh')
            self.assertEqual(invoke(entry, 'install', '--yes')['status'], 'IDEMPOTENT_PASS')
            self.assertEqual(invoke(entry, 'uninstall', '--yes')['status'], 'UNINSTALLED')
            self.assertFalse(entry.exists())
            self.assertIn('# keep', (home / 'config.toml').read_text(encoding='utf-8'))

    def test_project_refresh_recovers_same_day_failure_without_touching_other_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / 'codex home'
            project = base / 'project'
            other_project = base / 'other project'
            home.mkdir()
            project.mkdir()
            other_project.mkdir()
            now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
            fake_codex = base / 'codex'
            fake_codex.write_text('', encoding='utf-8')
            fake_codex.chmod(0o755)

            with patch.dict(os.environ, {'TZ': 'UTC'}, clear=False), \
                    patch.object(policy, 'FetchSources', return_value=[]), \
                    patch.object(policy, 'DirectSupportedEfforts',
                                 side_effect=policy.PolicyError('catalog unavailable')):
                first = policy.select(home, project, now=now)
            self.assertEqual(first['status'], 'blocked')

            with patch.dict(os.environ, {'TZ': 'UTC'}, clear=False), \
                    patch.object(policy, 'FetchSources', return_value=[]), \
                    patch.object(policy, 'DirectSupportedEfforts', return_value=list(policy.EFFORTS)):
                other = policy.select(home, other_project, now=now)
            self.assertEqual(other['role'], 'adaptive_luna_xhigh')
            other_cache = other_project / '.codex-adaptive-agents/state/policy-cache.json'
            other_before = other_cache.read_bytes()

            snapshot = {
                'schema': 1,
                'checked_at': policy._stamp(now),
                'supported_efforts': list(policy.EFFORTS),
                'root_model': 'gpt-6-astra',
                'child_model': 'gpt-6-luna',
                'codex': str(fake_codex),
                'cli_version': 'codex-cli 0.147.0',
                'evidence': 'public_client_catalogue',
            }

            def fake_refresh(selected_home, _codex, *, now=None):
                resource = Path(selected_home) / policy.RESOURCE
                resource.mkdir(parents=True, exist_ok=True)
                policy._atomic(policy._snapshot_path(Path(selected_home)),
                               policy._encode(snapshot))
                return snapshot

            calls = []
            original_select = policy.select
            fixed_now = now

            def fixed_clock_select(selected_home, selected_project=None, *, force=False, now=None, codex=None):
                calls.append({
                    'home': str(selected_home),
                    'project': None if selected_project is None else str(selected_project),
                    'force': force,
                    'codex': codex,
                })
                return original_select(selected_home, selected_project, force=force,
                                       now=now if now is not None else fixed_now, codex=codex)

            output = StringIO()
            with patch.dict(os.environ, {'TZ': 'UTC'}, clear=False), \
                    patch.object(install, 'validate', return_value={'schema': 4}), \
                    patch.object(cli, 'resolve_codex', return_value=str(fake_codex)), \
                    patch.object(policy, 'refresh_capabilities', side_effect=fake_refresh), \
                    patch.object(policy, 'DirectSupportedEfforts', return_value=list(policy.EFFORTS)), \
                    patch.object(policy, 'FetchSources', return_value=[]), \
                    patch.object(policy, 'select', side_effect=fixed_clock_select), \
                    redirect_stdout(output):
                refresh_rc = cli.main([
                    '--codex-home', str(home),
                    'refresh',
                    '--project', str(project),
                ])

            self.assertEqual(refresh_rc, 0)
            self.assertEqual(json.loads(output.getvalue())['role'], 'adaptive_luna_xhigh')
            self.assertEqual(calls, [{
                'home': str(home),
                'project': str(project),
                'force': True,
                'codex': str(fake_codex),
            }])
            self.assertFalse((home / policy.RESOURCE / 'state/policy-cache.json').exists())
            self.assertEqual(other_cache.read_bytes(), other_before)

            with patch.dict(os.environ, {'TZ': 'UTC'}, clear=False), \
                    patch.object(policy, 'FetchSources', return_value=[]), \
                    patch.object(policy, 'DirectSupportedEfforts',
                                 side_effect=AssertionError('same-day select must use project cache')):
                recovered = original_select(home, project, now=now)
            self.assertEqual(recovered['role'], 'adaptive_luna_xhigh')
            self.assertFalse(recovered['refreshed'])


if __name__ == '__main__':
    unittest.main()
