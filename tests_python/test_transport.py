from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_adaptive_agents import install, policy, transport, verify


PASSING_TAGS = r'''#!/usr/bin/env python3
import sys

seen = set()
for raw in sys.stdin.buffer:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    raw = raw.strip(b" \t\r")
    raw = bytes(c + 32 if 65 <= c <= 90 else c for c in raw)
    if raw and raw not in seen:
        seen.add(raw)
        sys.stdout.buffer.write(raw + b"\n")
'''
PASSING_NUMBERS = r'''#!/usr/bin/env python3
import re
import sys

seen = set()
for raw in sys.stdin.buffer:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not re.fullmatch(br"[+-]?[0-9]+", raw):
        raise SystemExit(2)
    try:
        value = int(raw.decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError, OverflowError):
        raise SystemExit(2)
    if value < -10000 or value > 10000:
        raise SystemExit(2)
    if value not in seen:
        seen.add(value)
        print(value)
'''


class TransportTests(unittest.TestCase):
    def test_thread_start_does_not_override_root_defaults_unless_requested(self):
        default = transport.thread_start_request('/tmp/project')
        self.assertNotIn('model', default)
        self.assertNotIn('model_reasoning_effort', default['config'])
        self.assertEqual(default['modelProvider'], 'openai')
        self.assertFalse(default['allowProviderModelFallback'])

        explicit = transport.thread_start_request('/tmp/project', 'gpt-6.1-sol', 'ultra')
        self.assertEqual(explicit['model'], 'gpt-6.1-sol')
        self.assertEqual(explicit['config']['model_reasoning_effort'], 'ultra')
        self.assertFalse(explicit['allowProviderModelFallback'])

    def test_live_root_model_identity_never_gates_selection_or_turn_start(self):
        effective_values = (
            {'model': 'gpt-5.6-sol', 'modelProvider': 'openai', 'reasoningEffort': 'high'},
            {'model': 'gpt-6-luna', 'modelProvider': 'openai', 'reasoningEffort': None},
            {'model': 'future-model', 'modelProvider': 'openai'},
            {'model': None, 'modelProvider': 'openai'},
            {'modelProvider': 'openai'},
        )
        for effective in effective_values:
            with self.subTest(effective=effective), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                project, output = base / 'project', base / 'evidence'
                calls, selector_calls = [], []

                class FakeSession:
                    def __init__(self, *_args):
                        self.root = ''
                        self.children = set()
                        self.events = []
                        self.completed = False
                        self.turn_started = False

                    def call(self, method, params):
                        calls.append(method)
                        if method == 'initialize':
                            return {}
                        if method == 'thread/start':
                            return {'thread': {'id': 'root'}, **effective}
                        if method == 'turn/start':
                            self.turn_started = True
                            raise RuntimeError('simulation stopped after turn/start; no provider call')
                        return {}

                    def keep(self, event):
                        self.events.append(event)

                    def send(self, _message):
                        pass

                    def close(self):
                        pass

                fake = FakeSession()
                def process(argv, **_kwargs):
                    if '--version' in argv:
                        return SimpleNamespace(returncode=0, stdout=transport.__version__.encode(), stderr=b'')
                    return SimpleNamespace(returncode=0, stdout=b'{"ok":false}', stderr=b'')

                def select_role():
                    selector_calls.append(())
                    return 'adaptive_luna_xhigh'

                diagnostic = StringIO()
                with patch.object(transport, 'Session', return_value=fake), \
                        patch.object(transport, 'run_process', side_effect=process):
                    with redirect_stderr(diagnostic):
                        result = transport.run(base / 'home', 'codex', project, output, None,
                                               select_role=select_role)
                self.assertEqual(result['status'], 'LIVE_FAILED')
                self.assertIn('simulation stopped after turn/start', result['error'])
                self.assertTrue(fake.turn_started)
                self.assertEqual(calls, ['initialize', 'thread/start', 'turn/start'])
                self.assertEqual(selector_calls, [()])
                self.assertIn('Starting two native gpt-6-luna tasks', diagnostic.getvalue())

    def test_fake_session_full_public_validation_and_artifact_failures(self):
        for mode, expected_status in (("success", "LIVE_VERIFIED"),
                                      ("malformed-child-metadata", "LIVE_FAILED"),
                                      ("foreign-child-provider", "LIVE_FAILED"),
                                      ("broken-artifact", "LIVE_FAILED")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                project, output = base / 'project', base / 'evidence'
                instances = []
                check_results = []

                class FakeSession:
                    def __init__(self, *_args):
                        self.root = ''
                        self.children = set()
                        self.events = []
                        self.completed = False
                        self.request = None
                        self.clock = 0.0
                        self.turn_started = False
                        instances.append(self)

                    def keep(self, event):
                        elapsed = event.pop('elapsed_seconds', None)
                        if elapsed is None:
                            self.clock += 0.1
                            elapsed = self.clock
                        else:
                            self.clock = max(self.clock, elapsed)
                        self.events.append({**event, 'elapsed_seconds': elapsed})

                    def call(self, method, params):
                        if method == 'initialize':
                            return {}
                        if method == 'thread/start':
                            self.request = params
                            return {'thread': {'id': 'root'}, 'model': 'gpt-6.1-sol',
                                    'modelProvider': 'openai', 'reasoningEffort': 'ultra'}
                        if method == 'turn/start':
                            self.turn_started = True
                            self.keep({'event': 'turn/started', 'threadId': 'root',
                                       'turn': {'id': 'root-turn', 'status': 'inProgress'},
                                       'elapsed_seconds': 0.5})
                            for child, start in (('child-a', 1.0), ('child-b', 2.0)):
                                self.children.add(child)
                                self.keep({'event': 'item/started', 'threadId': 'root', 'item': {
                                    'id': 'activity-' + child, 'type': 'subAgentActivity',
                                    'agentThreadId': child}, 'elapsed_seconds': start - 0.1})
                                self.keep({'event': 'thread/started', 'thread': {
                                    'id': child, 'parentThreadId': 'root', 'agentRole': 'adaptive_luna_xhigh',
                                    'status': {'type': 'active'}, 'spawn': {
                                        'parent_thread_id': 'root', 'agent_role': 'adaptive_luna_xhigh', 'depth': 1}},
                                    'elapsed_seconds': start - 0.05})
                                self.keep({'event': 'thread/status/changed', 'threadId': child,
                                           'status': {'type': 'active'}, 'elapsed_seconds': start})
                                self.keep({'event': 'turn/started', 'threadId': child,
                                           'turn': {'id': 'turn-' + child, 'status': 'inProgress'},
                                           'elapsed_seconds': start})
                            for child, end in (('child-a', 4.0), ('child-b', 5.0)):
                                self.keep({'event': 'turn/completed', 'threadId': child,
                                           'turn': {'id': 'turn-' + child, 'status': 'completed'},
                                           'elapsed_seconds': end})
                                self.keep({'event': 'thread/status/changed', 'threadId': child,
                                           'status': {'type': 'idle'}, 'elapsed_seconds': end + 0.1})
                            self.keep({'event': 'turn/completed', 'threadId': 'root',
                                       'turn': {'id': 'root-turn', 'status': 'completed'},
                                       'elapsed_seconds': 5.5})
                            self.completed = True
                            for name, contents in (('normalize_tags.py', PASSING_TAGS),
                                                   ('unique_numbers.py', PASSING_NUMBERS)):
                                (project / name).write_text(contents, encoding='utf-8')
                            if mode == 'broken-artifact':
                                (project / 'normalize_tags.py').write_text('# broken child artifact\n', encoding='utf-8')
                            return {}
                        if method == 'thread/read':
                            child = params['threadId']
                            metadata = {'id': child, 'parentThreadId': 'root',
                                'agentRole': 'adaptive_luna_xhigh', 'model': 'gpt-6-luna',
                                'reasoningEffort': 'xhigh', 'modelProvider': 'openai',
                                'status': {'type': 'idle'},
                                'spawn': {'parent_thread_id': 'root', 'agent_role': 'adaptive_luna_xhigh', 'depth': 1}}
                            if mode == 'malformed-child-metadata' and child == 'child-a':
                                metadata.update(model=0, reasoningEffort=False,
                                    effective=['malformed known-field shape'],
                                    requested={'model': 'gpt-6-astra', 'reasoningEffort': 'low'})
                            if mode == 'foreign-child-provider' and child == 'child-a':
                                metadata['modelProvider'] = 'third-party'
                            return {'thread': metadata}
                        return {}

                    def send(self, _message):
                        pass

                    def receive(self):
                        pass

                    def close(self):
                        pass

                def fake_process(argv, **_kwargs):
                    if '--version' in argv:
                        return SimpleNamespace(returncode=0, stdout=transport.__version__.encode(), stderr=b'')
                    if 'internal-smoke-check' in argv:
                        project_path = Path(argv[argv.index('--project') + 1])
                        checked = verify.check(project_path)
                        check_results.append(checked)
                        return SimpleNamespace(returncode=0 if checked.get('ok') else 1,
                            stdout=transport.encode(checked), stderr=b'')
                    raise AssertionError('unexpected fake subprocess command')

                diagnostic = StringIO()
                with patch.object(transport, 'Session', side_effect=FakeSession), \
                        patch.object(transport, 'run_process', side_effect=fake_process):
                    with redirect_stderr(diagnostic):
                        result = transport.run(base / 'home', 'codex', project, output, None,
                            root_model='gpt-6.1-sol', root_effort='ultra',
                            select_role=lambda: 'adaptive_luna_xhigh')

                self.assertEqual(result['status'], expected_status, result)
                fake = instances[0]
                self.assertEqual(fake.request['model'], 'gpt-6.1-sol')
                self.assertEqual(fake.request['config']['model_reasoning_effort'], 'ultra')
                self.assertFalse(fake.request['allowProviderModelFallback'])
                self.assertTrue(fake.turn_started)
                self.assertEqual(result['root_requested']['model'], 'gpt-6.1-sol')
                self.assertEqual(result['root_requested']['reasoningEffort'], 'ultra')
                if mode == 'success':
                    self.assertTrue(result['verification']['ok'], result)
                    self.assertTrue(result['independent_test']['ok'], result)
                    self.assertEqual(result['verification']['overlap_seconds'], 2.0)
                elif mode == 'broken-artifact':
                    self.assertEqual(result['error'], 'independent acceptance failed')
                    self.assertGreaterEqual(len(check_results), 2)
                    self.assertFalse(check_results[-1]['ok'], check_results[-1])
                else:
                    self.assertFalse(result['verification']['ok'], result)

    def test_live_requested_effective_mismatch_stops_before_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            calls = []

            class FakeSession:
                def __init__(self, *_args):
                    self.root = ''
                    self.children = set()
                    self.events = []
                    self.completed = False

                def call(self, method, params):
                    calls.append(method)
                    if method == 'initialize':
                        return {}
                    if method == 'thread/start':
                        return {'thread': {'id': 'root'}, 'model': 'gpt-6.1-sol',
                                'modelProvider': 'openai', 'reasoningEffort': 'high'}
                    raise AssertionError('requested/effective mismatch must stop before turn')

                def keep(self, event): self.events.append(event)
                def send(self, _message): pass
                def close(self): pass

            def before_gate_process(argv, **_kwargs):
                if '--version' in argv:
                    return SimpleNamespace(returncode=0, stdout=transport.__version__.encode(), stderr=b'')
                return SimpleNamespace(returncode=0, stdout=b'{"ok":false}', stderr=b'')
            with patch.object(transport, 'Session', return_value=FakeSession()), \
                    patch.object(transport, 'run_process', side_effect=before_gate_process):
                result = transport.run(base / 'home', 'codex', base / 'project', base / 'evidence', None,
                    root_model='gpt-6-astra', root_effort='max', select_role=lambda _model: 'adaptive_luna_xhigh')
            self.assertEqual(result['status'], 'LIVE_FAILED')
            self.assertIn('effective model differs from requested', result['error'])
            self.assertEqual(calls, ['initialize', 'thread/start'])

    def test_raw_session_handle_projection_reaches_transport_validator(self):
        real_session_type = transport.Session

        def run_case(*, label, sibling="absent", nested="completed", child_conflict=False,
                     tool="spawnAgent",
                     model="gpt-6-luna", effort="xhigh", expected_role="adaptive_luna_xhigh",
                     expected="LIVE_VERIFIED"):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                project, output = base / 'project', base / 'evidence'
                home = base / 'home'
                codex = str(base / 'codex')
                instances = []

                if expected_role == install.TERRA_ROLE:
                    _, _, plan = install.build(home)
                    install.apply(home, plan['plan_id'])
                    resource = home / policy.RESOURCE
                    resource.mkdir(parents=True, exist_ok=True)
                    (resource / 'terra-capabilities.json').write_text(json.dumps({
                        'schema': 1,
                        'model': policy.TERRA_MODEL,
                        'checked_at': policy._stamp(policy._utc()),
                        'supported_efforts': ['low', 'medium', 'high', 'xhigh'],
                        'catalogue_status': 'available',
                        'codex': codex,
                        'cli_version': 'codex-cli 0.159.0',
                        'evidence': 'public_client_catalogue',
                    }), encoding='utf-8')

                class SyntheticSession(real_session_type):
                    def __init__(self, *_args):
                        self.root = ''
                        self.children = set()
                        self.events = []
                        self.completed = False
                        self.clock = 0.0
                        instances.append(self)

                    def keep(self, event):
                        self.clock += 0.1
                        self.events.append({**event, 'elapsed_seconds': self.clock})

                    def send(self, _message):
                        pass

                    def close(self):
                        pass

                    def public(self, method, params):
                        self.handle({'method': method, 'params': params})

                    def call(self, method, params):
                        if method == 'initialize':
                            return {}
                        if method == 'thread/start':
                            return {'thread': {'id': 'root'}, 'model': 'gpt-6.1-sol',
                                    'modelProvider': 'openai', 'reasoningEffort': 'ultra'}
                        if method == 'turn/start':
                            self.public('turn/started', {'threadId': 'root', 'turn': {
                                'id': 'root-turn', 'status': 'inProgress'}})
                            ids = ['child-a', 'child-b'] if tool == 'spawnAgent' else []
                            item = {'id': 'spawn-call', 'type': 'collabAgentToolCall',
                                    'tool': tool, 'status': 'inProgress', 'senderThreadId': 'root',
                                    'receiverThreadIds': ids, 'model': model, 'reasoningEffort': effort}
                            self.public('item/started', {'threadId': 'root', 'item': item})
                            self.public('item/completed', {'threadId': 'root', 'item': {
                                **item, 'status': 'completed'}})
                            for child in ('child-a', 'child-b'):
                                self.public('item/started', {'threadId': 'root', 'item': {
                                    'id': 'activity-' + child, 'type': 'subAgentActivity',
                                    'agentThreadId': child}})
                                spawn = {'parent_thread_id': 'root',
                                         'agent_role': expected_role, 'depth': 1}
                                self.public('thread/started', {'thread': {
                                    'id': child, 'parentThreadId': 'root',
                                    'agentRole': expected_role,
                                    'status': {'type': 'active'}, 'spawn': spawn}})
                                self.public('thread/status/changed', {'threadId': child,
                                                                      'status': {'type': 'active'}})
                                self.public('turn/started', {'threadId': child, 'turn': {
                                    'id': 'turn-' + child, 'status': 'inProgress'}})
                            for child in ('child-a', 'child-b'):
                                terminal_child = {'threadId': child, 'turn': {
                                    'id': 'turn-' + child, 'status': {'type': 'completed'}}}
                                if child_conflict and child == 'child-a':
                                    terminal_child['status'] = {'type': 'failed'}
                                self.public('turn/completed', terminal_child)
                                self.public('thread/status/changed', {'threadId': child,
                                                                      'status': {'type': 'idle'}})
                            root_turn = {'id': 'root-turn'}
                            if nested != 'absent':
                                root_turn['status'] = nested
                            terminal = {'threadId': 'root', 'turn': root_turn}
                            if sibling != 'absent':
                                terminal['status'] = sibling
                            self.public('turn/completed', terminal)
                            for name, contents in (('normalize_tags.py', PASSING_TAGS),
                                                   ('unique_numbers.py', PASSING_NUMBERS)):
                                (project / name).write_text(contents, encoding='utf-8')
                            return {}
                        if method == 'thread/read':
                            child = params['threadId']
                            return {'thread': {
                                'id': child, 'parentThreadId': 'root',
                                'agentRole': expected_role,
                                'model': model if tool == 'spawnAgent' else 'gpt-6-luna',
                                'reasoningEffort': effort if tool == 'spawnAgent' else 'xhigh',
                                'modelProvider': 'openai',
                                'status': {'type': 'idle'},
                                'spawn': {'parent_thread_id': 'root',
                                          'agent_role': expected_role, 'depth': 1}}}
                        return {}

                def fake_process(argv, **_kwargs):
                    if '--version' in argv:
                        return SimpleNamespace(returncode=0,
                                               stdout=transport.__version__.encode(), stderr=b'')
                    if 'internal-smoke-check' in argv:
                        project_path = Path(argv[argv.index('--project') + 1])
                        checked = verify.check(project_path)
                        return SimpleNamespace(returncode=0 if checked.get('ok') else 1,
                                               stdout=transport.encode(checked), stderr=b'')
                    raise AssertionError('unexpected synthetic subprocess')

                self.assertIs(SyntheticSession.handle, real_session_type.handle)
                with patch.object(transport, 'Session', SyntheticSession), \
                        patch.object(transport, 'run_process', side_effect=fake_process), \
                        patch.object(policy, '_run_codex', return_value=SimpleNamespace(
                            returncode=0, stdout=b'codex-cli 0.159.0\n', stderr=b'')):
                    result = transport.run(home, codex, project, output,
                                           expected_role if expected_role == install.TERRA_ROLE else None,
                                           root_model='gpt-6.1-sol', root_effort='ultra',
                                           select_role=lambda: expected_role)
                self.assertEqual(result['status'], expected, (label, result))
                root_end = next(event for event in result['events']
                                if event.get('event') == 'turn/completed'
                                and event.get('threadId') == 'root')
                if sibling != 'absent' and isinstance(sibling, dict):
                    self.assertEqual(root_end['status'], {'type': sibling.get('type')})
                if isinstance(nested, dict):
                    self.assertEqual(root_end['turn']['status'], nested)
                if expected == 'LIVE_VERIFIED':
                    self.assertTrue(result['verification']['ok'], result)
                    self.assertEqual(result['selected_role'], expected_role)
                    selected_model, selected_effort = verify.role_model_effort(expected_role)
                    self.assertEqual(result['selected_role_model'], selected_model)
                    self.assertEqual(result['selected_role_effort'], selected_effort)
                    self.assertFalse(result['gui_operation_verified'])
                else:
                    self.assertFalse(result.get('verification', {}).get('ok', True), result)
                return result

        for sibling, nested in (({'type': 'failed'}, 'completed'),
                                ('failed', {'type': 'completed'})):
            with self.subTest(sibling=sibling, nested=nested):
                result = run_case(label='conflicting statuses', sibling=sibling, nested=nested,
                                  expected='LIVE_FAILED')
                self.assertIn('turn status fields conflict', result['verification']['errors'])
        self.assertTrue(run_case(label='matching mapped statuses', sibling={'type': 'completed'},
                                 nested={'type': 'completed'})['verification']['ok'])
        self.assertTrue(run_case(label='missing nested status uses sibling',
                                 sibling={'type': 'completed'}, nested='absent')['verification']['ok'])
        terra = run_case(label='Terra dispatch identity only', model='gpt-6.1-terra', effort='xhigh',
                         expected_role='adaptive_terra_xhigh')
        self.assertFalse(terra['gui_operation_verified'])
        child_conflict_result = run_case(label='child conflicting statuses', child_conflict=True,
                                         expected='LIVE_FAILED')
        self.assertIn('turn status fields conflict', child_conflict_result['verification']['errors'])
        run_case(label='missing sibling status')
        run_case(label='null sibling status', sibling=None)
        for field, value in (('model', {'slug': 'gpt-6-luna'}), ('reasoningEffort', 5),
                             ('model', '   '), ('reasoningEffort', '\t ')):
            kwargs = {'model': 'gpt-6-luna', 'effort': 'xhigh'}
            kwargs['model' if field == 'model' else 'effort'] = value
            with self.subTest(field=field, value=value):
                result = run_case(label='malformed spawn claim', expected='LIVE_FAILED', **kwargs)
                error_field = 'model' if field == 'model' else 'effort'
                self.assertTrue(any(f'spawn requested {error_field} has unsupported public shape' == error
                                    for error in result['verification']['errors']), result)
        run_case(label='non-spawn malformed claims', tool='sendInput', model={'bad': True},
                 effort='  ')

    def test_metadata_drops_content_and_preserves_native_lineage(self):
        result = transport.metadata({'id':'child','parentThreadId':'root','agentRole':'adaptive_luna_max',
            'model':'gpt-6-luna','reasoningEffort':'max','modelProvider':'openai',
            'preview':'must not persist','turns':['must not persist'],'sessionId':'must not persist',
            'status':{'type':'idle','extra':'private'},
            'effective':{'model':'gpt-6-luna','reasoningEffort':'max','prompt':'must not persist'},
            'requested':{'model':'gpt-6-luna','reasoningEffort':'max','token':'must not persist'},
            'source':{'subagent':{'thread_spawn':{'parent_thread_id':'root','agent_role':'adaptive_luna_max','depth':1,'prompt':'private'}}}})
        self.assertNotIn('preview', result)
        self.assertNotIn('turns', result)
        self.assertNotIn('sessionId', result)
        self.assertEqual(result['model'], 'gpt-6-luna')
        self.assertEqual(result['reasoningEffort'], 'max')
        self.assertEqual(result['modelProvider'], 'openai')
        self.assertEqual(result['effective'], {'model':'gpt-6-luna','reasoningEffort':'max'})
        self.assertEqual(result['requested'], {'model':'gpt-6-luna','reasoningEffort':'max'})
        self.assertEqual(result['spawn'],dict(parent_thread_id='root',agent_role='adaptive_luna_max',depth=1))
        self.assertEqual(result['status'], {'type':'idle'})

    def test_stream_only_keeps_lifecycle(self):
        s = object.__new__(transport.Session)
        s.root='root';s.children=set();s.events=[];s.completed=False;s.started=0
        s.handle({'method':'item/agentMessage/delta','params':{'delta':'do not persist'}})
        self.assertEqual(s.events, [])
        s.handle({'method':'item/started','params':{'threadId':'root','item':{
            'id':'call','type':'subAgentActivity','agentThreadId':'child','text':'do not persist'}}})
        self.assertEqual(s.children, {'child'})
        self.assertNotIn('text', s.events[0]['item'])
        s.handle({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'turn','status':'completed','items':['private']}}})
        self.assertTrue(s.completed)
        self.assertNotIn('items', s.events[-1]['turn'])

    def test_server_approval_request_is_rejected(self):
        s=object.__new__(transport.Session)
        sent=[]
        s.send=sent.append
        with self.assertRaisesRegex(RuntimeError,'approval'):
            s.handle({'id':1,'method':'item/commandExecution/requestApproval','params':{}})
        self.assertIn('error',sent[0])

    def test_scope_extra_file_or_links_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for name in ('AGENTS.md','contract.md','normalize_tags.py','unique_numbers.py'):
                (root/name).write_text('fixture',encoding='utf-8')
            self.assertEqual(len(transport.hashes(root)),4)
            (root/'extra.py').write_text('unexpected',encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError,'scope'):
                transport.hashes(root)

    def test_invalid_run_never_starts_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            with patch.object(transport,'Session',side_effect=AssertionError('unexpected model call')):
                result=transport.run(root,'codex',root/'project',root/'project'/'evidence','adaptive_luna_max')
                terra=transport.run(root,'codex',root/'terra-project',root/'terra-evidence',
                                    install.TERRA_ROLE)
            self.assertEqual(result['status'],'LIVE_FAILED')
            self.assertFalse((root/'project').exists())
            self.assertEqual(terra['status'],'LIVE_FAILED')
            self.assertIn('Terra role is not installed', terra['error'])
            self.assertFalse((root/'terra-project').exists())


if __name__=='__main__':
    unittest.main()
