import hashlib
import ast
import http.server
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('firecrawl_update.sh')


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'GA401 Bash owner required')
class ResearchUpdaterTests(unittest.TestCase):
    def test_snapshot_refuses_missing_recovery_image_before_writing_manifest(self):
        body = SOURCE.read_text().split("<<'PYSNAPSHOT'\n", 1)[1].split('\nPYSNAPSHOT', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'container-ids').write_text('a' * 64)
            for name in ('docker-compose.yaml', 'docker-compose.override.yaml'):
                (root / name).touch()
            data = {'service': 'api', 'image': 'sha256:' + 'b' * 64,
                    'compose_files': ','.join(str(root / name) for name in
                                              ('docker-compose.yaml', 'docker-compose.override.yaml')),
                    'project': 'firecrawl'}
            def fake_run(args, **kwargs):
                if args[:2] == ['docker', 'inspect']:
                    return SimpleNamespace(stdout=json.dumps(data), returncode=0)
                if args[:3] == ['docker', 'image', 'inspect']:
                    return SimpleNamespace(returncode=1)
                raise AssertionError(args)
            with patch.object(sys, 'argv', ['snapshot', str(root)]), patch.object(Path, 'cwd', return_value=root), \
                    patch.object(subprocess, 'run', side_effect=fake_run):
                with self.assertRaisesRegex(SystemExit, 'recovery image unavailable'):
                    exec(compile(body, 'snapshot', 'exec'), {})
            self.assertFalse((root / 'compose-images.json').exists())

    def test_prior_image_only_overlays_can_differ_between_one_shot_and_live_services(self):
        body = SOURCE.read_text().split("<<'PYSNAPSHOT'\n",1)[1].split('\nPYSNAPSHOT',1)[0]
        tree = ast.parse(body)
        function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='baseline_files')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/'state'; state.mkdir()
            source=ast.get_source_segment(body,function).replace('/home/marck/.local/state/firecrawl-updater',str(state))
            namespace={'Path':Path,'json':json,'re':re}
            exec(source,namespace)
            base=[str(root/name) for name in ('docker-compose.yaml','docker-compose.override.yaml')]
            for name,filename in (('candidate.old','images.json'),
                                  ('candidate.new','images.candidate-patch-promotion-'+'c'*32+'.json')):
                folder=state/name;folder.mkdir(); overlay=folder/filename
                overlay.write_text(json.dumps({'services':{'api':{'image':'sha256:'+'a'*64,'pull_policy':'never'}}}))
                with patch.object(Path,'cwd',return_value=root):
                    self.assertEqual(namespace['baseline_files'](base+[str(overlay)]),base)
                    overlay.write_text(json.dumps({'services':{'api':{'image':'sha256:'+'a'*64,'environment':{'UNSAFE':'1'}}}}))
                    with self.assertRaises(SystemExit): namespace['baseline_files'](base+[str(overlay)])

    def test_security_refresh_and_promotion_overlays_are_accepted_only_when_image_only(self):
        body = SOURCE.read_text().split("<<'PYSNAPSHOT'\n",1)[1].split('\nPYSNAPSHOT',1)[0]
        tree = ast.parse(body)
        function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='baseline_files')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/'state'; security=root/'security'
            source=(ast.get_source_segment(body,function)
                    .replace('/home/marck/.local/state/firecrawl-updater',str(state))
                    .replace('/home/marck/.local/state/firecrawl-security-refresh',str(security)))
            namespace={'Path':Path,'json':json,'re':re}
            exec(source,namespace)
            base=[str(root/name) for name in ('docker-compose.yaml','docker-compose.override.yaml')]
            overlays=[]
            for folder,name,value in ((state/('brotli-promotion-'+'1'*32),'core-candidate.json',
                                       {'services':{'api':{'image':'sha256:'+'a'*64,'pull_policy':'never'},
                                                    'redis':{'image':'sha256:'+'b'*64,'pull_policy':'never'}}}),
                                      (security/('run-'+'2'*32),'firecrawl-recovery.json',{'services':{'api':{'image':'sha256:'+'c'*64}}}),
                                      (security/('run-'+'3'*32),'firecrawl-candidate.json',{'services':{'api':{'image':'sha256:'+'d'*64}}})):
                folder.mkdir(parents=True); (folder/name).write_text(json.dumps(value)); overlays.append(folder/name)
            with patch.object(Path,'cwd',return_value=root):
                self.assertEqual(namespace['baseline_files'](base+[str(p) for p in overlays]),base)
                self.assertEqual(namespace['baseline_files'](base),base)
                overlays[1].write_text(json.dumps({'services':{'api':{'image':'sha256:'+'c'*64,'environment':{'X':'1'}}}}))
                with self.assertRaises(SystemExit): namespace['baseline_files'](base+[str(overlays[1])])
                overlays[1].write_text(json.dumps({'services':{'api':{'image':'sha256:'+'c'*64,'pull_policy':'always'}}}))
                with self.assertRaises(SystemExit): namespace['baseline_files'](base+[str(overlays[1])])
                stray=security/('run-'+'4'*32)/'firecrawl-monitor-candidate.json'; stray.parent.mkdir()
                stray.write_text(json.dumps({'services':{'api':{'image':'sha256:'+'e'*64}}}))
                with self.assertRaises(SystemExit): namespace['baseline_files'](base+[str(stray)])
                with self.assertRaises(SystemExit): namespace['baseline_files']([base[1],base[0]])

    def test_standalone_updater_has_no_gateway_or_app_dependency(self):
        source = SOURCE.read_text().lower()
        self.assertNotIn('strata', source)
        self.assertNotIn('systemctl', source)
        self.assertNotIn('public_web.py', source)
        self.assertIn('guards/$guard_sha256.py', source)

    def test_standalone_smoke_checks_guard_search_and_scrape(self):
        body = SOURCE.read_text().split("<<'PYSMOKE'\n", 1)[1].split('\nPYSMOKE', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); guard = root/'guard.py'
            guard.write_text('def verify():\n    return {"verified": True}\n'); guard.chmod(0o600)
            digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            calls = []
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    calls.append((self.path, data))
                    if self.path == '/v2/search':
                        payload = {'success':True,'data':{'web':[{'url':'https://example.com'}]}}
                    else:
                        payload = {'success':True,'data':{'markdown':'sample','metadata':{'statusCode':200}}}
                    raw = json.dumps(payload).encode()
                    self.send_response(200); self.send_header('Content-Length',str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                def log_message(self, *_): pass
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            endpoint = 'http://127.0.0.1:' + str(server.server_port)
            args = [sys.executable,'-B','-c',body,str(guard),digest,endpoint]
            result = subprocess.run(args,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual([path for path,_ in calls],['/v2/search','/v2/scrape'])
            self.assertEqual({key:calls[1][1][key] for key in ('fastMode','maxAge','storeInCache')},
                             {'fastMode':True,'maxAge':0,'storeInCache':False})
            guard.write_text('def verify():\n    return {"verified": False}\n')
            self.assertNotEqual(subprocess.run(args,capture_output=True,text=True,timeout=10).returncode,0)
            args[5] = hashlib.sha256(guard.read_bytes()).hexdigest()
            failure = subprocess.run(args,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(failure.returncode,0)
            self.assertIn('guard_rejected',failure.stderr)
            self.assertEqual(len(calls),2)

    def run_owner(self, root, scenario='current', background=False):
        repo=root/'repo'; repo.mkdir(exist_ok=True); (repo/'.env').touch()
        state=root/'state'; state.mkdir(exist_ok=True)
        script = r"""
source "$1"
repo="$2/repo"; state_dir="$2/state"; scenario="$3"
record() { printf '%s\n' "$*" >> "$state_dir/calls"; }
set_restart_policy() { record policy; }
validate_config() { record config; }
sync_fork_main() { record fork-sync; }
git() {
  case "$1 $2" in
    'status --porcelain') ;;
    'fetch --prune'|'fetch --tags'|'merge-base --is-ancestor') ;;
    'tag --list') printf 'v2.11.999\n' ;;
    'rev-list -n') printf '%040d\n' 2 ;;
    'rev-parse HEAD') if [[ "$scenario" == current ]]; then printf '%040d\n' 2; else printf '%040d\n' 1; fi ;;
    'describe --tags') printf 'v2.11.518\n' ;;
    'checkout --detach') record "checkout $3" ;;
    *) return 77 ;;
  esac
}
prepare_candidate() {
  record candidate
  if [[ "$scenario" == prepare-pause ]]; then
    touch "$state_dir/preparing"
    for _ in $(seq 1 100); do
      [[ -f "$state_dir/continue" ]] && return 0
      sleep 0.05
    done
    return 1
  fi
  [[ "$scenario" != admission-fail ]]
}
snapshot_images() { record snapshot; }
verify_candidate_manifest() { record manifest; [[ "$scenario" != manifest-fail ]]; }
docker() { record deploy; [[ "$scenario" != rollback-fail && "$scenario" != deploy-fail ]]; }
restore_images() { record restore; [[ "$scenario" != rollback-fail ]]; }
smoke_test() { record smoke; }
main
"""
        args=['bash','-c',script,'fixture',str(SOURCE),str(root),scenario]
        if background: return subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        return subprocess.run(args,capture_output=True,text=True,timeout=15)

    def test_preparation_allows_readers_serializes_runs_and_defers_busy_cutover(self):
        import fcntl
        import time
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); child=self.run_owner(root,'prepare-pause',background=True)
            try:
                deadline=time.monotonic()+5
                while not (root/'state/preparing').exists():
                    self.assertIsNone(child.poll())
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.02)
                with (root/'state/update.lock').open('rb') as reader:
                    fcntl.flock(reader, fcntl.LOCK_SH)
                    with (root/'state/update.lock').open('rb') as installer:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(installer,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    second=self.run_owner(root,'compatible')
                    self.assertEqual(second.returncode,0)
                    self.assertIn('already running',second.stdout)
                    (root/'state/continue').touch()
                    output,error=child.communicate(timeout=8)
                    self.assertNotEqual(child.returncode,0,error)
                    self.assertIn('active page reads deferred cutover',output)
                    calls=(root/'state/calls').read_text()
                    self.assertNotIn('deploy',calls); self.assertNotIn('checkout',calls)
                self.assertFalse((root/'state/deployed.env').exists())
            finally:
                if child.poll() is None: child.kill(); child.communicate()

    def test_same_version_repeated_checks_do_not_build_switch_or_deploy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for _ in range(2):
                result=self.run_owner(root)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('already current at v2.11.999',result.stdout)
            calls=(root/'state/calls').read_text().splitlines()
            self.assertEqual(calls.count('smoke'),2)
            self.assertFalse(any(x.startswith(('candidate','checkout','snapshot','deploy','restore')) for x in calls))
            self.assertIn('tag=v2.11.999',(root/'state/deployed.env').read_text())

    def test_compatible_new_release_switches_only_after_gate_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); result=self.run_owner(root,'compatible')
            self.assertEqual(result.returncode,0,result.stderr)
            calls=(root/'state/calls').read_text().splitlines()
            self.assertLess(calls.index('candidate'),calls.index('snapshot'))
            self.assertLess(calls.index('snapshot'),next(i for i,x in enumerate(calls) if x.startswith('checkout')))
            self.assertLess(calls.index('manifest'),calls.index('deploy'))
            self.assertLess(calls.index('deploy'),calls.index('smoke'))
            self.assertNotIn('restore',calls)
            self.assertIn('tag=v2.11.999',(root/'state/deployed.env').read_text())

    def test_gate_failure_preserves_old_runtime_and_success_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'state').mkdir(); marker=root/'state/deployed.env'; marker.write_text('old-success')
            result=self.run_owner(root,'admission-fail')
            self.assertNotEqual(result.returncode,0)
            calls=(root/'state/calls').read_text()
            self.assertNotIn('checkout',calls); self.assertNotIn('deploy',calls); self.assertNotIn('snapshot',calls)
            self.assertEqual(marker.read_text(),'old-success')

    def test_manifest_failure_restores_source_before_any_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); result=self.run_owner(root,'manifest-fail')
            self.assertNotEqual(result.returncode,0)
            calls=(root/'state/calls').read_text().splitlines()
            self.assertEqual(sum(x.startswith('checkout') for x in calls),2)
            self.assertNotIn('deploy',calls); self.assertNotIn('restore',calls)
            self.assertFalse((root/'state/deployed.env').exists())

    def test_cutover_failure_rolls_back_and_failure_is_not_reported_as_success(self):
        for scenario in ('deploy-fail','rollback-fail'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root=Path(temp); result=self.run_owner(root,scenario)
                self.assertNotEqual(result.returncode,0)
                calls=(root/'state/calls').read_text().splitlines()
                self.assertIn('restore',calls)
                self.assertEqual(sum(x.startswith('checkout') for x in calls),2)
                self.assertFalse((root/'state/deployed.env').exists())
                if scenario=='rollback-fail':
                    self.assertIn('rollback failed: exact image preimage could not be restored',result.stdout)
                    self.assertNotIn('rollback restored previous',result.stdout)
                else:
                    self.assertIn('rollback restored previous',result.stdout)
                    self.assertIn('smoke',calls)

    def test_installer_preserves_a_changed_updater_and_records_only_exact_replacement(self):
        from deployment import install_firecrawl_update as owner
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'firecrawl-update'; target.write_bytes(b'original')
            lock = root / 'update.lock'; lock.touch(); lock.chmod(0o600)
            guard = root / 'transport.py'; guard.write_bytes(b'# reviewed transport guard\n')
            digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            source = root / 'candidate.sh'; source.write_bytes(('#!/usr/bin/env bash\nguard_sha256='+digest+'\nexit 0\n').encode())
            with patch.object(owner, 'ROOT', root), patch.object(owner, 'TARGET', target), patch.object(owner, 'install_restart_override'):
                with self.assertRaisesRegex(ValueError, 'Installed updater changed'):
                    owner.install(source, '0' * 64, guard)
                self.assertEqual(target.read_bytes(), b'original')
                guard.write_bytes(b'# changed\n')
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    owner.install(source, hashlib.sha256(b'original').hexdigest(), guard)
                guard.write_bytes(b'# reviewed transport guard\n')
                result = owner.install(source, hashlib.sha256(b'original').hexdigest(), guard)
                self.assertEqual(result['status'], 'installed')
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual((root/'guards'/f'{digest}.py').read_bytes(), guard.read_bytes())
                self.assertEqual(next(root.glob('*.before.sh')).read_bytes(), b'original')
                self.assertEqual(json.loads(next(root.glob('*.json')).read_text())['status'], 'installed')
