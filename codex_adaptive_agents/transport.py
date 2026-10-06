"""Bounded public Codex stdio verification; no persistent service or history reads."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import re
import sys
import subprocess
import threading
import time
import tomllib

from . import __version__, install
from .platform import atomic, clean_env, encode, finish, loads, read, run as run_process, safe, spawn, stop


def metadata(thread):
    # Share the verifier's canonical public projection.  The fixed invalid
    # marker preserves malformed known fields without retaining their values.
    from . import verify
    return verify.normalize_public_thread_metadata(thread)


def thread_start_request(project, root_model=None, root_effort=None):
    """Build thread/start parameters without inventing root defaults."""
    request={'cwd':str(project),'ephemeral':True,'modelProvider':'openai',
        'sandbox':'workspace-write','approvalPolicy':'never','allowProviderModelFallback':False,
        'experimentalRawEvents':False,'config':{'agents.enabled':True}}
    if root_model is not None:
        request['model']=root_model
    if root_effort is not None:
        request['config']['model_reasoning_effort']=root_effort
    return request


class Session:
    def __init__(self, codex, project, env, config, deadline):
        args = [codex, 'app-server', '--listen', 'stdio://', '--disable', 'plugins', '--disable', 'apps',
                '--disable', 'memories', '-c', 'allow_managed_hooks_only=true']
        for name in sorted(config.get('mcp_servers', {})):
            if not re.fullmatch('[A-Za-z0-9_-]+', name):
                raise RuntimeError('MCP name unsafe for temporary override')
            args += ['-c', 'mcp_servers.' + name + '.enabled=false']
        self.proc = spawn(args, cwd=project, env=env)
        self.deadline = deadline
        self.started = time.monotonic()
        self.events, self.children = [], set()
        self.root, self.completed, self.sequence = '', False, 0
        self.messages = queue.Queue(maxsize=32)
        self.cancelled = threading.Event()
        self.failure = None
        self.threads = [threading.Thread(target=self.reader, daemon=True),
                        threading.Thread(target=self.stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def reader(self):
        total = 0
        try:
            while not self.cancelled.is_set():
                data = self.proc.stdout.readline(2*1024*1024 + 1)
                if not data:
                    break
                total += len(data)
                if len(data) > 2*1024*1024 or total > 64*1024*1024:
                    self.failure = RuntimeError('public stream size limit exceeded')
                    break
                while not self.cancelled.is_set():
                    try:
                        self.messages.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass
        finally:
            if not self.cancelled.is_set():
                self.failure = self.failure or RuntimeError('official public stream ended')

    def stderr(self):
        total = 0
        try:
            while not self.cancelled.is_set():
                data = self.proc.stderr.read1(16384)
                if not data:
                    break
                total += len(data)
                if total > 8*1024*1024:
                    self.failure = RuntimeError('official diagnostic stream exceeded limit')
                    break
        except (OSError, ValueError):
            pass

    def keep(self, value):
        value['elapsed_seconds'] = time.monotonic() - self.started
        self.events.append(value)
        if len(self.events) > 12000:
            raise RuntimeError('public lifecycle event limit exceeded')

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n')
        self.proc.stdin.flush()

    def receive(self, deadline=None):
        deadline = min(deadline or self.deadline, self.deadline)
        while True:
            if self.failure and self.messages.empty():
                raise self.failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('bounded public event wait expired')
            try:
                raw = self.messages.get(timeout=min(0.25, remaining))
                break
            except queue.Empty:
                continue
        message = loads(raw)
        if not isinstance(message, dict):
            raise RuntimeError('invalid public message')
        self.handle(message)
        return message

    def call(self, method, params):
        self.sequence += 1
        wanted = self.sequence
        self.send(dict(id=wanted, method=method, params=params))
        deadline = time.monotonic() + 30
        while True:
            message = self.receive(deadline)
            if message.get('id') == wanted:
                if message.get('error') is not None:
                    raise RuntimeError('public request rejected: ' + method)
                result = message.get('result')
                if not isinstance(result, dict):
                    raise RuntimeError('invalid public result')
                return result

    def handle(self, message):
        method = message.get('method')
        if not method:
            return
        if 'id' in message:
            self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Verification grants no approvals or tools'}})
            raise RuntimeError('unexpected server request; no approval granted')
        params = message.get('params') or {}
        tid = params.get('threadId')
        if method == 'thread/started':
            thread = metadata(params.get('thread') or {})
            if thread.get('id'):
                self.children.add(thread['id'])
            self.keep(dict(event=method, thread=thread))
        elif method in ('thread/status/changed', 'turn/started', 'turn/completed'):
            value = dict(event=method, threadId=tid)
            if 'status' in params:
                status = params['status']
                value['status'] = {'type': status.get('type')} if isinstance(status, dict) else status
            if isinstance(params.get('turn'), dict):
                turn = params['turn']
                value['turn'] = {k: turn[k] for k in ('id', 'status') if k in turn}
                if method == 'turn/completed' and tid == self.root:
                    self.completed = True
                    from . import verify
                    if verify._turn_status(value) != 'completed':
                        raise RuntimeError('root turn failed')
            self.keep(value)
        elif method in ('item/started', 'item/completed'):
            item = params.get('item') or {}
            kind = item.get('type')
            if kind == 'subAgentActivity':
                if item.get('agentThreadId'):
                    self.children.add(item['agentThreadId'])
                reduced = {k: item.get(k) for k in ('id', 'type', 'agentPath', 'agentThreadId', 'kind')}
                self.keep(dict(event=method, threadId=tid, item=reduced))
            elif kind == 'collabAgentToolCall':
                reduced = {k: item.get(k) for k in ('id','type','tool','status','senderThreadId','receiverThreadIds','model','reasoningEffort')}
                reduced['agentStatuses'] = {k:v.get('status') for k,v in (item.get('agentsStates') or {}).items() if isinstance(v,dict)}
                self.children.update(item.get('receiverThreadIds') or [])
                self.keep(dict(event=method, threadId=tid, item=reduced))
        elif method == 'error':
            self.keep(dict(event='error', threadId=tid, error_recorded=True))
            raise RuntimeError('official runtime reported an error')

    def close(self):
        try:
            self.proc.stdin.close()
            try:
                code = self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stop(self.proc)
            else:
                if code != 0:
                    raise RuntimeError("official transport exited unsuccessfully")
        finally:
            self.cancelled.set()
            stop(self.proc)
            for thread in self.threads:
                thread.join(timeout=1)
            for stream in (self.proc.stdout, self.proc.stderr):
                stream.close()


def contains(parent, child):
    return child == parent or parent in child.parents


def hashes(project):
    wanted = {'AGENTS.md','contract.md','normalize_tags.py','unique_numbers.py'}
    found = set()
    for path in project.rglob('*'):
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('fixture link or reparse point rejected')
        if path.is_file():
            relative = path.relative_to(project).as_posix()
            if relative.startswith('.codex-adaptive-agents/'):
                continue
            found.add(relative)
        elif not path.is_dir():
            raise RuntimeError('nonregular fixture rejected')
    if found != wanted:
        raise RuntimeError('fixture scope differs')
    return {name: install.sha(read(project / name)) for name in sorted(wanted)}


def run(home, codex, project, output, role=None, *, root_model=None, root_effort=None, select_role=None):
    from . import verify
    home, project, output = (Path(x) for x in (home, project, output))
    entry = Path(__file__).resolve().parents[1] / 'codex-adaptive-agents.py'
    result = dict(status='LIVE_FAILED', version=__version__, project=str(project), evidence=str(output),
                  server_reported_model='unknown', internal_session_or_auth_files_read=False,
                  turn_history_requested=False, test_transport='official transient stdio; no daemon or TCP listener')
    result['root_requested']={'model':root_model,'reasoningEffort':root_effort,
                              'modelProvider':'openai','allowProviderModelFallback':False}
    started = time.monotonic()
    deadline = started + 600
    session = None
    prepared = False
    try:
        if role is not None and role not in ('adaptive_luna_' + e for e in install.EFFORTS):
            raise RuntimeError('unsupported role')
        if root_effort is not None and (not isinstance(root_effort,str) or not root_effort.strip()):
            raise RuntimeError('requested root effort must be nonempty')
        if contains(project, output) or contains(output, project) or contains(project, entry):
            raise RuntimeError('project, evidence and trusted code must be separate')
        for path in (project/'AGENTS.md', output/'result.json', entry):
            safe(path)
        if output.exists() or (project.exists() and any(project.iterdir())):
            raise RuntimeError('verification project and output must be new or empty')
        output.mkdir(parents=True, mode=0o700)
        project.mkdir(parents=True, exist_ok=True, mode=0o700)
        prepared = True
        assets = entry.parent / 'codex_adaptive_agents/assets'
        for name in ('contract.md','normalize_tags.py','unique_numbers.py'):
            atomic(project/name, read(assets/name))
        atomic(project/'AGENTS.md', read(assets/'contract.md'))
        baseline = hashes(project)
        trusted = {name:install.sha(read(entry.parent/name)) for name in install.RUNTIME_FILES}
        env = clean_env()
        env['CODEX_HOME'] = str(home)
        sandbox = [codex,'sandbox','-P',':workspace','-C',str(project),'--',sys.executable,'-I','-B',str(entry)]
        def independent():
            response = run_process(sandbox+['internal-smoke-check','--project',str(project)],
                                   cwd=project,env=env,timeout=min(90,max(0.1,deadline-time.monotonic())))
            value=loads(response.stdout)
            if not isinstance(value,dict) or not isinstance(value.get('ok'),bool):
                raise RuntimeError('independent validator response invalid')
            if response.returncode != 0 and value['ok']:
                raise RuntimeError('independent validator exit mismatch')
            return value
        initial=independent()
        if initial['ok']:
            raise RuntimeError('initial stubs unexpectedly passed')
        probe=run_process(sandbox+['--version'],cwd=project,env=env,timeout=15)
        if probe.returncode or probe.stdout.strip()!=__version__.encode():
            raise RuntimeError('workspace sandbox positive control failed')
        result['initial_stubs_failed']=True
        config=tomllib.loads((read(home/'config.toml') or b'').decode('utf-8'))
        session=Session(codex,project,env,config,deadline)
        session.call('initialize',{'clientInfo':{'name':'codex_adaptive_agents_verify','version':__version__},'capabilities':{'experimentalApi':True}})
        session.send({'method':'initialized','params':{}})
        thread_request=thread_start_request(project,root_model,root_effort)
        root=session.call('thread/start',thread_request)
        session.root=(root.get('thread') or {}).get('id','')
        effective={k:root[k] for k in ('model','modelProvider','provider','reasoningEffort','reasoning_effort') if k in root}
        effective, shape_errors = verify._normalize_effective(effective, 'root client effective metadata')
        if shape_errors:
            raise RuntimeError(shape_errors[0])
        requested=result['root_requested']
        session.keep(dict(event='root/client-requested',threadId=session.root,metadata=requested))
        session.keep(dict(event='root/client-effective',threadId=session.root,metadata=effective))
        if not session.root:
            raise RuntimeError('root thread/start returned no thread id')
        providers=[effective[key] for key in ('modelProvider','provider') if key in effective and effective[key] is not None]
        if not providers or any(provider != 'openai' for provider in providers):
            raise RuntimeError('root client provider is not openai')
        effective_model=effective.get('model')
        effective_effort=effective.get('reasoningEffort', effective.get('reasoning_effort'))
        if root_model is not None and effective_model != root_model:
            raise RuntimeError('root client effective model differs from requested model')
        if root_effort is not None and effective_effort != root_effort:
            raise RuntimeError('root client effective effort differs from requested effort')
        if role is None:
            if not callable(select_role):
                raise RuntimeError('child role selector unavailable')
            selection=select_role()
            if isinstance(selection,dict):
                raise RuntimeError('child role selection unavailable')
            role=selection
        if role not in ('adaptive_luna_' + e for e in install.EFFORTS):
            raise RuntimeError('child role selection unavailable')
        prompt=('Follow contract.md. Spawn exactly two direct native leaf subagents concurrently using agent_type '+role+
            '. You must not implement the functions yourself. One child owns only '+str(project/'normalize_tags.py')+
            ' and the other owns only '+str(project/'unique_numbers.py')+'. Give both the full contract, exact file ownership and absolute cwd '+str(project)+
            '. State that other collaborators are present and their work must be preserved. Children must not delegate or launch CLI workers. '+
            'Use Python 3.11 standard library only; no shell-specific scripts, Go, dependencies, network, sleeps, extra files or tests. '+
            'AGENTS.md and contract.md are immutable. The selector already selected '+role+'; do not rerun it. '+
            'Wait until both children complete and stop writing. The external verifier runs independent tests after your final reply. '+
            'Do not add reports or caches. This explicitly authorizes two real native Luna tasks.')
        print('Starting two native Luna tasks (up to 600 seconds); this uses Codex allowance.', file=sys.stderr)
        session.call('turn/start',{'threadId':session.root,'input':[{'type':'text','text':prompt}]})
        while not session.completed:
            session.receive()
        session.children.discard(session.root)
        if len(session.children)!=2:
            raise RuntimeError('expected exactly two native children')
        for child in sorted(session.children):
            params={'threadId':child,'includeTurns':False}
            session.keep(dict(event='public-request',method='thread/read',params=params))
            value=session.call('thread/read',params)
            session.keep(dict(event='child/public-metadata',thread=metadata(value.get('thread') or {})))
        checks=verify.validate_public_events(session.events,role,
                    requested_root_model=root_model,requested_root_effort=root_effort)
        result['verification']=checks
        if checks.get('ok') is not True:
            raise RuntimeError('public lifecycle validation failed')
        after=hashes(project)
        if any(after[name]!=baseline[name] for name in ('AGENTS.md','contract.md')):
            raise RuntimeError('protected smoke contract changed')
        if any(after[name]==baseline[name] for name in ('normalize_tags.py','unique_numbers.py')):
            raise RuntimeError('smoke stub unchanged')
        finalcheck=independent()
        if finalcheck.get('ok') is not True:
            raise RuntimeError('independent acceptance failed')
        if hashes(project)!=after:
            raise RuntimeError('artifacts changed during validation')
        if trusted!={name:install.sha(read(entry.parent/name)) for name in install.RUNTIME_FILES}:
            raise RuntimeError('trusted verifier source changed')
        result.update(status='LIVE_VERIFIED',independent_test=finalcheck,artifact_sha256=after,selected_role=role)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        result['error']=str(error) if type(error) is RuntimeError else 'verification input or stream failed'
    finally:
        if session:
            try:
                session.close()
            except Exception:
                result.update(status='LIVE_FAILED',error='official transport cleanup failed')
            result['events']=session.events
        result['elapsed_seconds']=time.monotonic()-started
        if prepared:
            atomic(output/'public-events.json',encode(result.get('events',[])))
            atomic(output/'result.json',encode(result))
    return result
