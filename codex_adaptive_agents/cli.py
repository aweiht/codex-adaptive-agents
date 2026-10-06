"""One portable command for setup, policy and independent verification."""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from . import __version__, install
from .platform import atomic, clean_env, encode, loads, read, run


def resolve_codex(home, explicit=None):
    # Snapshot metadata may describe the prior probe, but it is not authority
    # to execute a path. Resolve an explicit caller choice or normal PATH entry.
    value = explicit or 'codex'
    found = shutil.which(value)
    if not found:
        raise ValueError('official Codex CLI required; install it and run codex login')
    return os.path.abspath(found)


def doctor(home, codex=None):
    from . import policy
    result = dict(version=__version__, codex_home=str(home), status='NOT_READY',
                  checks={}, errors=[], live_verified=False, verification_level='offline_installation_only')
    try:
        receipt = install.validate(home)
        if receipt is None:
            raise ValueError('not installed; run install')
        if receipt['schema'] != 4 or receipt.get('version') != __version__:
            raise ValueError('installation version differs; run install to upgrade')
        install.build(home, uninstall=True)
        raw = read(home / 'config.toml')
        _, changes = install.transform(raw, 'merge', desired=install.desired(raw))
        if changes:
            raise ValueError('managed agent defaults differ from installation')
        result['checks']['managed_files_and_config'] = 'PASS'
        command = resolve_codex(home, codex)
        result['checks']['capabilities'] = policy.doctor_capabilities(home, command)
        if result['checks']['capabilities'].get('status') != 'READY':
            raise ValueError('public capability snapshot or CLI unavailable; run refresh')
        cached = policy.cached_select(home)
        if cached.get('role') not in ('adaptive_luna_' + e for e in install.EFFORTS):
            raise ValueError('policy cache unavailable or stale; run refresh')
        result['checks']['policy'] = cached
        env = clean_env()
        env['CODEX_HOME'] = str(home)
        version = run([sys.executable, '-B', str(home / install.ENTRY), '--version'], env=env, timeout=8)
        if version.returncode or version.stdout.strip().decode() != __version__:
            raise ValueError('installed Python entry version mismatch')
        result['status'] = 'READY_FALLBACK' if cached.get('status') == 'fallback' else 'READY'
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        result['errors'] = [str(error) if type(error) in (ValueError, RuntimeError) else 'input or file operation failed']
    return result


def parser():
    value = argparse.ArgumentParser(description='Codex Adaptive Agents: Python 3.11+, standard library only.')
    value.add_argument('command', nargs='?', choices=('install', 'uninstall', 'recover', 'rollback',
        'doctor', 'select', 'refresh', 'verify', 'internal-smoke-check',
        'process-init', 'process-run', 'process-check', 'process-cleanup'))
    value.add_argument('--version', action='version', version=__version__)
    value.add_argument('--codex-home')
    value.add_argument('--codex', help='official Codex CLI path')
    value.add_argument('--project')
    value.add_argument('--output')
    value.add_argument('--backup', help='recorded transaction path; required for rollback only')
    value.add_argument('--plan-id')
    value.add_argument('--yes', action='store_true', help='apply the managed, backed-up change')
    value.add_argument('--dry-run', action='store_true', help='preview install or uninstall without applying')
    value.add_argument('--explain', action='store_true')
    value.add_argument('--live', action='store_true', help='use real Codex allowance for two native tasks')
    value.add_argument('--root-model', help='optional verify --live request override; ignored by select')
    value.add_argument('--root-effort', help='optional root reasoning effort override for verify --live')
    value.add_argument('--run-id', help='process handoff run returned by process-init')
    value.add_argument('--owner', help='work package or child Agent identifier')
    value.add_argument('--purpose', help='short process purpose; do not include secrets')
    value.add_argument('--timeout', type=float, help='process-run timeout in seconds (default 600)')
    value.add_argument('--summary', action='store_true', help='store bounded process output in private logs')
    value.add_argument('--retain', help='comma-separated entry IDs explicitly kept running')
    return value


def show(value):
    print(encode(value).decode('utf-8'), end='')


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    child_argv = None
    if '--' in arguments:
        split = arguments.index('--')
        child_argv, arguments = arguments[split + 1:], arguments[:split]
    seen = set()
    for arg in arguments:
        if arg.startswith('--'):
            key = arg.split('=', 1)[0]
            if key in seen:
                show(dict(status='ERROR', error='duplicate command option'))
                return 1
            seen.add(key)
    cli = parser()
    args = cli.parse_args(arguments)
    if args.command is None:
        cli.print_help()
        return 0
    home = Path(os.path.abspath(os.path.expanduser(args.codex_home or os.environ.get('CODEX_HOME') or '~/.codex')))
    try:
        command = args.command
        # Reject incompatible safety flags before reading state or invoking any
        # command boundary: a preview request must never perform a live action.
        if args.dry_run and command not in ('install', 'uninstall'):
            raise ValueError('--dry-run is only valid for install or uninstall')
        if args.yes and args.dry_run:
            raise ValueError('--yes and --dry-run cannot be combined')
        if args.backup is not None and command != 'rollback':
            raise ValueError('--backup is only valid for rollback')
        if command == 'rollback' and not args.backup:
            raise ValueError('rollback requires --backup PATH')
        if args.live and command != 'verify':
            raise ValueError('--live is only valid for verify')
        if args.root_model is not None and command not in ('select', 'verify'):
            raise ValueError('--root-model is only valid for select or verify')
        if args.root_effort is not None and command != 'verify':
            raise ValueError('--root-effort is only valid for verify')
        if args.root_effort is not None and not args.root_effort.strip():
            raise ValueError('--root-effort must be nonempty')
        if child_argv is not None and command != 'process-run':
            raise ValueError('command arguments after -- are only valid for process-run')
        if command.startswith('process-'):
            from . import process_registry
            if not args.project:
                raise ValueError('--project is required for process commands')
            if args.summary and command != 'process-run':
                raise ValueError('--summary is only valid for process-run')
            if args.live or args.yes or args.dry_run or args.plan_id or args.backup or args.output or args.explain:
                raise ValueError('unsupported option for process command')
            project = Path(os.path.abspath(os.path.expanduser(args.project)))
            if command == 'process-init':
                if args.run_id or args.owner or args.purpose or args.timeout is not None or args.retain or args.summary:
                    raise ValueError('process-init only accepts --project')
                result = process_registry.init_run(project)
            else:
                if not args.run_id:
                    raise ValueError('--run-id is required')
                if command == 'process-run':
                    if not args.owner or not args.purpose or not child_argv or args.retain:
                        raise ValueError('process-run requires --owner, --purpose and -- COMMAND ARGS')
                    timeout = args.timeout if args.timeout is not None else 600
                    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
                        raise ValueError('--timeout must be greater than zero and at most 86400 seconds')
                    def started(event):
                        print(encode(event).decode('utf-8'), end='', file=sys.stderr, flush=True)
                    result = process_registry.run_process(project, args.run_id, args.owner,
                        args.purpose, child_argv, timeout=timeout, on_start=started,
                        summary=args.summary)
                else:
                    if args.owner or args.purpose or args.timeout is not None:
                        raise ValueError('--owner, --purpose and --timeout are only valid for process-run')
                    retain = tuple(args.retain.split(',')) if args.retain is not None else ()
                    if any(not item for item in retain) or len(set(retain)) != len(retain):
                        raise ValueError('--retain requires unique nonempty entry IDs')
                    action = process_registry.check_run if command == 'process-check' else process_registry.cleanup_run
                    result = action(project, args.run_id, retain=retain)
            show(result)
            return 0 if result.get('status') in ('STARTED', 'READY', 'SUCCESS', 'CLEAN', 'RETAINED') else 1
        if args.run_id or args.owner or args.purpose or args.timeout is not None or args.retain or args.summary:
            raise ValueError('process options require a process command')
        if command == 'internal-smoke-check':
            from . import verify
            if not args.project:
                raise ValueError('--project is required')
            result = verify.check(Path(os.path.abspath(args.project)))
            show(result)
            return 0 if result.get('ok') else 1
        if command in ('recover', 'rollback'):
            if not args.yes:
                raise ValueError('recover/rollback requires --yes')
            show(install.recover(home, args.backup))
            return 0
        if command in ('install', 'uninstall'):
            uninstall = command == 'uninstall'
            _, _, plan = install.build(home, uninstall)
            if args.dry_run or not args.yes:
                show(plan)
                return 0
            expected = args.plan_id or plan['plan_id']
            snapshot = None
            if not uninstall:
                from . import policy
                codex = resolve_codex(home, args.codex)
                snapshot = policy.preflight(home, codex)
            result = install.apply(home, expected, uninstall)
            if snapshot is not None:
                atomic(home / install.RESOURCE / 'capabilities.json', encode(snapshot))
                try:
                    selected = policy.select(home, force=True, codex=codex)
                    if selected.get('role') not in ('adaptive_luna_' + e for e in install.EFFORTS):
                        raise ValueError('no supported selection')
                except (RuntimeError, ValueError, OSError):
                    result.update(status='INSTALLED_NEEDS_REFRESH', next_step='run refresh', live_verified=False)
                    show(result)
                    return 1
                result.update(policy=selected, live_verified=False,
                    standalone_entrypoint=str(home / install.ENTRY),
                    next_step='open a new Codex task; doctor or verify --live')
            show(result)
            return 0
        from . import policy
        if command == 'doctor':
            result = doctor(home, args.codex)
            show(result)
            return 0 if result['status'].startswith('READY') else 1
        if command in ('select', 'refresh', 'verify'):
            receipt = install.validate(home)
            if receipt is None or receipt['schema'] != 4:
                raise ValueError('Python edition not installed; run install')
            if os.environ.get('ORCHESTRATOR_WORKER') == '1':
                raise ValueError('leaf workers must not refresh or delegate')
        if command == 'select':
            project = Path(os.path.abspath(args.project or os.getcwd()))
            codex = resolve_codex(home, args.codex) if args.codex else None
            selected = policy.select(home, project, codex=codex)
            if selected.get('role') not in ('adaptive_luna_' + e for e in install.EFFORTS):
                raise ValueError('selection unavailable; run refresh; no role dispatched')
            if args.explain:
                show(selected)
            else:
                print(selected['role'])
            return 0
        if command == 'refresh':
            codex = resolve_codex(home, args.codex)
            policy.refresh_capabilities(home, codex)
            if args.project:
                project = Path(os.path.abspath(args.project))
                selected = policy.select(home, project, force=True, codex=codex)
            else:
                selected = policy.select(home, force=True, codex=codex)
            if selected.get('role') not in ('adaptive_luna_' + e for e in install.EFFORTS):
                raise ValueError('selection unavailable after refresh')
            show(selected)
            return 0
        if command == 'verify':
            if not args.live:
                raise ValueError('verify requires --live; it uses real Codex allowance')
            codex = resolve_codex(home, args.codex)
            readiness = doctor(home, codex)
            if not readiness['status'].startswith('READY'):
                show(readiness)
                return 1
            from . import verify
            if args.project:
                project = Path(os.path.abspath(args.project))
            else:
                project = Path(tempfile.mkdtemp(prefix='codex-adaptive-agents-verify-')).resolve() / 'project'
            output = Path(os.path.abspath(args.output)) if args.output else project.parent / ('verification-' + str(time.time_ns()))
            def select_role():
                selected = policy.select(home, project, codex=codex)
                if selected.get('role') not in ('adaptive_luna_' + e for e in install.EFFORTS):
                    return selected
                return selected['role']
            result = verify.run(home, codex, project, output, None,
                                root_model=args.root_model, root_effort=args.root_effort,
                                select_role=select_role)
            show(result)
            return 0 if result.get('status') == 'LIVE_VERIFIED' else 1
        raise ValueError('unknown command')
    except KeyboardInterrupt:
        show(dict(status='INTERRUPTED'))
        return 130
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        # TOML/JSON parser errors may include user content; never print it.
        if type(error) not in (ValueError, RuntimeError):
            message = 'input or file operation failed; inspect paths and managed changes'
        else:
            message = str(error)
        show(dict(status='ERROR', error=message))
        return 1
