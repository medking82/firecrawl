"""Execute the actual candidate gate with synthetic Docker transport, never production data."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'deployment/firecrawl_update.sh'
GUARD = ROOT / 'deployment/firecrawl_transport_guard.py'


def embedded(marker):
    text = SOURCE.read_text()
    return text.split("<<'" + marker + "'\n", 1)[1].split('\n' + marker, 1)[0]


@unittest.skipUnless(os.name == 'posix', 'Native GA401 process tests')
class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'; self.app.mkdir()
        self.guard = self.app / GUARD.name
        shutil.copyfile(GUARD, self.guard); self.guard.chmod(0o600)
        self.guard_hash = hashlib.sha256(self.guard.read_bytes()).hexdigest()
        spec = importlib.util.spec_from_file_location('oracle_guard', GUARD)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        profile = module.TRANSPORT_PROFILES['v2.11.473']
        self.syntax = {'syntax_hashes': {}}
        self.sandbox_tree = {'just_bash_tree_sha256': '0' * 64, 'file_count': 977}
        self.tree = {'undici_lib_sha256': module.TRANSPORT_PROFILES['v2.11.621']['undici_lib_sha256'], 'file_count': 116}
        self.probe = {'hashes':copy.deepcopy(profile['hashes']), 'dependencies':copy.deepcopy(profile['dependencies']),
            'predicates':dict(module.EXPECTED_PREDICATES), 'safeguards':dict.fromkeys(
                ['private_bypass_absent_or_empty','db_auth_canonical_false','dotenv_absent',
                 'alternate_dotenv_unconfigured','expected_working_directory','node_options_unconfigured'], True), 'hosted_bash_present': False}
        self.config = {'name':'firecrawl','services':{'api':{'build':{'context':'candidate'},
            'environment':{'USE_DB_AUTHENTICATION':'false','PLAYWRIGHT_MICROSERVICE_URL':'http://fixture'}},
            'nuq-postgres':{}, 'playwright-service':{}}}
        self.packages = {'decode-uri-component':[{'location':'/app/a','version':'0.5.1'}], 'systeminformation':[{'location':'/app/b','version':'5.33.18'}]}
        self.metadata = {'Env':[], 'Cmd':['node','dist/src/harness.js','--start-docker'],
            'Entrypoint':['docker-entrypoint.sh'],'WorkingDir':'/app','User':''}
        self.evidence = self.root / 'evidence'; self.evidence.mkdir()
        self.preimage = self.root / 'preimage'; self.preimage.mkdir()
        (self.preimage / 'compose-images.json').write_text(json.dumps({'services': {
            'nuq-postgres': {'image': 'sha256:'+'d'*64, 'pull_policy':'never'},
            'playwright-service': {'image': 'sha256:'+'e'*64, 'pull_policy':'never'}}}))
        self.fixture = self.root / 'fixture.json'
        self.write_fixture()
        docker = self.root / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
from pathlib import Path
p=Path(os.environ['CANDIDATE_FIXTURE']); data=json.loads(p.read_text()); args=sys.argv[1:]
with p.with_suffix('.calls').open('a') as log: log.write(json.dumps(args)+'\\n')
if args[:2]==['compose','--project-name'] or args[:2]==['compose','config']: print(json.dumps(data['config']))
elif args[:2]==['image','inspect']: print(json.dumps([{'Id':args[2] if args[2].startswith('sha256:') else 'sha256:'+'a'*64,'Config':data['metadata']}]))
elif args[:2]==['inspect','--format'] and args[-1] in ('firecrawl-nuq-postgres-1','firecrawl-playwright-service-1'):
 name='nuq-postgres' if args[-1]=='firecrawl-nuq-postgres-1' else 'playwright-service'
 print(json.dumps({'image':'sha256:'+('d' if name=='nuq-postgres' else 'e')*64,'service':name,'project':'firecrawl'}))
elif args[:2]==['run','--rm']: print(json.dumps(data['packages']))
elif args[:2]==['run','-d']: print('b'*64)
elif args[0]=='inspect': print(json.dumps({'id':'b'*64,'image':'sha256:'+'a'*64,'running':True}))
elif args[0]=='exec':
 if 'syntax_hashes' in args[-1]: value=data['syntax']
 elif 'just_bash_tree_sha256' in args[-1]: value=data['sandbox_tree']
 elif 'undici_lib_sha256' in args[-1]: value=data['tree']
 else: value=data['probe']
 print(json.dumps(value))
elif args[:2]==['rm','-f']: pass
else: raise SystemExit('unexpected Docker action')
''')
        docker.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.root)+os.pathsep+os.environ['PATH'], CANDIDATE_FIXTURE=str(self.fixture))

    def write_fixture(self):
        self.fixture.write_text(json.dumps({'config':self.config,'probe':self.probe,'tree':self.tree,'metadata':self.metadata,'packages':self.packages,
            'syntax':self.syntax,'sandbox_tree':self.sandbox_tree}))

    def candidate(self, tag='v2.11.473'):
        self.write_fixture()
        result = subprocess.run([sys.executable,'-B','-c',embedded('PYCANDIDATE'),str(self.guard),self.guard_hash,str(self.root),str(self.root),tag,'c'*40,str(self.evidence),'sha256:'+'a'*64],env=self.env,capture_output=True,text=True,timeout=30)
        self.checks_hash = hashlib.sha256((self.evidence/'checks.json').read_bytes()).hexdigest()
        return result

    def manifest(self):
        self.write_fixture()
        return subprocess.run([sys.executable,'-B','-c',embedded('PYMANIFEST'),str(self.evidence),str(self.guard),self.guard_hash,'c'*40,self.checks_hash,str(self.preimage)],env=self.env,capture_output=True,text=True,timeout=30)

    def test_exact_reviewed_profile_pins_images_only_after_recheck(self):
        result=self.candidate(); self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.evidence/'manifest.json').exists())
        result=self.manifest(); self.assertEqual(result.returncode,0,result.stderr)
        manifest=json.loads((self.evidence/'manifest.json').read_text())
        self.assertEqual(manifest['images']['api']['image'],'sha256:'+'a'*64)
        self.assertEqual(manifest['images']['nuq-postgres']['image'],'sha256:'+'d'*64)
        self.assertEqual(manifest['images']['playwright-service']['image'],'sha256:'+'e'*64)
        calls=[json.loads(line) for line in self.fixture.with_suffix('.calls').read_text().splitlines()]
        probe=next(row for row in calls if row[:2]==['run','-d'])
        self.assertIn('none',probe); self.assertIn('--read-only',probe)
        self.assertFalse(any('up' in row or 'build' in row for row in calls))

    def test_changed_protected_image_rejects_cutover(self):
        self.assertEqual(self.candidate().returncode, 0)
        path = self.preimage / 'compose-images.json'
        value = json.loads(path.read_text())
        value['services']['nuq-postgres']['image'] = 'sha256:'+'f'*64
        path.write_text(json.dumps(value))
        result = self.manifest()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Protected recovery image changed', result.stderr)
        self.assertFalse((self.evidence/'manifest.json').exists())

    def test_changed_hash_cannot_publish_manifest(self):
        self.probe['hashes']['/app/dist/src/lib/exchange.js']='f'*64
        self.assertNotEqual(self.candidate().returncode,0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['status'],'awaiting_transport_review')
        self.assertNotEqual(self.manifest().returncode,0)
        self.assertFalse((self.evidence/'manifest.json').exists())

    def test_new_tag_reuses_exact_transport_without_claiming_release_review(self):
        result=self.candidate('v2.11.999'); self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.manifest().returncode,0)
        report=json.loads((self.evidence/'manifest.json').read_text())
        self.assertEqual(report['tag'],'v2.11.999')
        self.assertEqual(report['review_basis']['profile'],'v2.11.473')
        self.assertEqual(report['transport_check']['transport_profile'],'v2.11.473')
        self.assertEqual(report['review_basis']['receipt_sha256'],report['transport_check']['receipt_sha256'])

    def test_629_profile_receipt_and_mixed_bytes(self):
        spec = importlib.util.spec_from_file_location('profile_guard', GUARD)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        profile = module.TRANSPORT_PROFILES['v2.11.629']
        receipt = ROOT / 'deployment/firecrawl-transport-20261030-v629.json'
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), profile['receipt_sha256'])
        self.assertNotEqual(profile['hashes']['/app/dist/src/controllers/v2/types.js'],
                            module.TRANSPORT_PROFILES['v2.11.625']['hashes']['/app/dist/src/controllers/v2/types.js'])
        self.probe['hashes'] = copy.deepcopy(profile['hashes'])
        self.probe['dependencies'] = copy.deepcopy(profile['dependencies'])
        self.assertEqual(self.candidate('v2.11.629').returncode, 0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['transport_check']['transport_profile'], 'v2.11.629')
        self.probe['hashes']['/app/dist/src/lib/exchange.js'] = 'f'*64
        self.assertNotEqual(self.candidate('v2.11.629').returncode, 0)

    def test_683_profile_receipt_and_mixed_bytes(self):
        spec = importlib.util.spec_from_file_location('profile_guard_683', GUARD)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        profile, previous = module.TRANSPORT_PROFILES['v2.11.683'], module.TRANSPORT_PROFILES['v2.11.629']
        receipt_path = ROOT / 'deployment/firecrawl-transport-20261105-v683.json'
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(hashlib.sha256(receipt_path.read_bytes()).hexdigest(), profile['receipt_sha256'])
        # The receipt names exactly the compiled files that differ from the audited baseline.
        changed = {key: value for key, value in profile['hashes'].items() if previous['hashes'][key] != value}
        self.assertEqual(changed, receipt['changed_compiled_hashes'])
        self.assertEqual(len(changed), 8)
        self.assertEqual((profile['dependencies'], profile['undici_lib_sha256']),
                         (receipt['dependencies'], receipt['undici_lib_sha256']))
        self.assertEqual(receipt['baseline_profile'], 'v2.11.629')
        self.probe['hashes'] = copy.deepcopy(profile['hashes'])
        self.probe['dependencies'] = copy.deepcopy(profile['dependencies'])
        self.assertEqual(self.candidate('v2.11.683').returncode, 0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['transport_check']['transport_profile'], 'v2.11.683')
        # One 629-era safeFetch beside the other 683 bytes is an unknown profile.
        self.probe['hashes']['/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js'] = \
            previous['hashes']['/app/dist/src/scraper/scrapeURL/engines/utils/safeFetch.js']
        self.assertNotEqual(self.candidate('v2.11.683').returncode, 0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['status'], 'awaiting_transport_review')

    def test_704_profile_receipt_and_mixed_bytes(self):
        spec = importlib.util.spec_from_file_location('profile_guard_704', GUARD)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        profile, previous = module.TRANSPORT_PROFILES['v2.11.704'], module.TRANSPORT_PROFILES['v2.11.683']
        receipt_path = ROOT / 'deployment/firecrawl-transport-20261109-v704.json'
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(hashlib.sha256(receipt_path.read_bytes()).hexdigest(), profile['receipt_sha256'])
        # The receipt names exactly the compiled files that differ from the audited baseline.
        changed = {key: value for key, value in profile['hashes'].items() if previous['hashes'][key] != value}
        self.assertEqual(changed, receipt['changed_compiled_hashes'])
        self.assertEqual(len(changed), 2)
        self.assertEqual((profile['dependencies'], profile['undici_lib_sha256']),
                         (receipt['dependencies'], receipt['undici_lib_sha256']))
        self.assertEqual(receipt['baseline_profile'], 'v2.11.683')
        self.probe['hashes'] = copy.deepcopy(profile['hashes'])
        self.probe['dependencies'] = copy.deepcopy(profile['dependencies'])
        self.assertEqual(self.candidate('v2.11.704').returncode, 0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['transport_check']['transport_profile'], 'v2.11.704')
        # One 683-era exchange beside the other 704 bytes is an unknown profile.
        self.probe['hashes']['/app/dist/src/lib/exchange.js'] = \
            previous['hashes']['/app/dist/src/lib/exchange.js']
        self.assertNotEqual(self.candidate('v2.11.704').returncode, 0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['status'], 'awaiting_transport_review')

    def test_new_tag_can_match_any_complete_supported_profile(self):
        spec=importlib.util.spec_from_file_location('supported_guard',GUARD)
        guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        for name,profile in guard.TRANSPORT_PROFILES.items():
            with self.subTest(profile=name):
                self.probe['hashes']=copy.deepcopy(profile['hashes'])
                self.probe['dependencies']=copy.deepcopy(profile['dependencies'])
                self.syntax = {'syntax_hashes': copy.deepcopy(profile.get('syntax_hashes', {}))}
                self.sandbox_tree['just_bash_tree_sha256'] = profile.get('just_bash_tree_sha256', '0' * 64)
                result=self.candidate('v2.11.999'); self.assertEqual(result.returncode,0,result.stderr)
                report=json.loads((self.evidence/'checks.json').read_text())
                self.assertEqual(report['review_basis']['profile'],name)

    def test_735_receipt_syntax_fallback_and_sandbox_tree(self):
        spec = importlib.util.spec_from_file_location('profile_guard_735', GUARD)
        assert spec is not None and spec.loader is not None
        guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        profile = guard.TRANSPORT_PROFILES['v2.11.735']
        receipt_path = ROOT / 'deployment/firecrawl-transport-20261113-v735.json'
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(hashlib.sha256(receipt_path.read_bytes()).hexdigest(), profile['receipt_sha256'])
        for key in ('hashes', 'syntax_hashes', 'dependencies', 'undici_lib_sha256', 'just_bash_tree_sha256'):
            self.assertEqual(receipt[key], profile[key])
        self.assertEqual(len(profile['syntax_hashes']), 22)
        self.probe['hosted_bash_present'] = True
        self.probe['hashes'] = copy.deepcopy(profile['hashes'])
        self.probe['dependencies'] = copy.deepcopy(profile['dependencies'])
        self.syntax = {'syntax_hashes': copy.deepcopy(profile['syntax_hashes'])}
        self.sandbox_tree['just_bash_tree_sha256'] = profile['just_bash_tree_sha256']
        self.assertEqual(self.candidate('v2.11.735').returncode, 0)
        self.assertEqual(self.manifest().returncode, 0)
        (self.evidence / 'manifest.json').unlink()  # A second candidate has fresh evidence in production.
        self.probe['hashes'][next(iter(profile['hashes']))] = '0' * 64
        self.assertEqual(self.candidate('v2.11.999').returncode, 0)
        report = json.loads((self.evidence / 'checks.json').read_text())
        self.assertEqual(report['transport_check']['fingerprint_match'], 'syntax')
        self.assertEqual(self.manifest().returncode, 0)
        self.sandbox_tree['just_bash_tree_sha256'] = 'f' * 64
        self.assertNotEqual(self.candidate('v2.11.999').returncode, 0)
        report = json.loads((self.evidence / 'checks.json').read_text())
        self.assertEqual(report['transport_check']['error'], 'dependency_tree_mismatch')
        self.assertNotEqual(self.manifest().returncode, 0)

    def test_621_undici_tree_drift_never_publishes_manifest(self):
        spec=importlib.util.spec_from_file_location('tree_guard',GUARD)
        guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        profile=guard.TRANSPORT_PROFILES['v2.11.621']
        self.probe['hashes']=copy.deepcopy(profile['hashes'])
        self.probe['dependencies']=copy.deepcopy(profile['dependencies'])
        self.tree['undici_lib_sha256']='f'*64
        self.assertNotEqual(self.candidate('v2.11.621').returncode,0)
        report=json.loads((self.evidence/'checks.json').read_text())
        self.assertEqual(report['status'],'awaiting_transport_review')
        self.assertEqual(report['transport_check']['error'],'dependency_tree_mismatch')
        self.assertFalse((self.evidence/'images.json').exists())
        self.assertNotEqual(self.manifest().returncode,0)
        self.assertFalse((self.evidence/'manifest.json').exists())

    def test_supported_changes_outside_transport_remain_bound_to_manifest(self):
        # A harmless API label/build-context change does not change audited code.
        self.config['services']['api']['labels']={'upstream.release':'new'}
        self.config['services']['api']['build']['context']='new-source'
        self.assertEqual(self.candidate('v2.11.999').returncode,0)
        self.assertEqual(self.manifest().returncode,0)

    def test_new_tag_rejects_changed_dependency_shape_and_safeguards(self):
        original=copy.deepcopy(self.probe)
        cases=[('dependencies','undici','8.0.0'),('dependencies','node_major','22'),
               ('safeguards','dotenv_absent',False),('predicates','ALLOW_LOCAL_WEBHOOKS_true',True)]
        for section,key,value in cases:
            with self.subTest(section=section,key=key):
                self.probe=copy.deepcopy(original); self.probe[section][key]=value
                self.assertNotEqual(self.candidate('v2.11.999').returncode,0)
                self.assertFalse((self.evidence/'images.json').exists())
        self.probe=copy.deepcopy(original); self.probe['hashes'].pop('/app/dist/src/lib/exchange.js')
        self.assertNotEqual(self.candidate('v2.11.999').returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())

    def test_new_tag_cannot_mix_individually_known_profile_hashes(self):
        spec=importlib.util.spec_from_file_location('mixed_guard',GUARD)
        guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        key='/app/dist/src/lib/exchange.js'
        self.probe['hashes'][key]=guard.TRANSPORT_PROFILES['v2.11.470']['hashes'][key]
        self.assertNotEqual(self.candidate('v2.11.999').returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())

    def test_linked_guard_still_rejected_before_candidate_probe(self):
        path=self.app/GUARD.name; path.unlink(); path.symlink_to(GUARD)
        self.assertNotEqual(self.candidate('v2.11.999').returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())
        self.assertNotIn('"run", "-d"',self.fixture.with_suffix('.calls').read_text())

    def test_unsafe_configuration_rejected_before_starting_probe(self):
        self.config['services']['api']['environment']['ALLOW_LOCAL_WEBHOOKS']='1'
        self.assertNotEqual(self.candidate().returncode,0)
        calls=self.fixture.with_suffix('.calls').read_text()
        self.assertNotIn('"run", "-d"',calls)

    def test_privileged_fields_and_injected_commands_fail_before_probe(self):
        cases = {key:'host' for key in ('pid','ipc','userns_mode','network_mode','user','cgroup_parent')}
        cases.update(cap_add=['SYS_ADMIN'], security_opt=['seccomp=unconfined'],
            tmpfs=['/app'], devices=['/dev/sda'], privileged=True,
            command=['node','--require','/tmp/x.js','dist/src/harness.js'],
            entrypoint=['sh','-c'], volumes=[{'type':'bind','source':'/tmp','target':'/app'}])
        for key,value in cases.items():
            with self.subTest(key=key):
                self.config['services']['api'][key]=value
                self.assertNotEqual(self.candidate().returncode,0)
                self.assertFalse((self.evidence/'images.json').exists())
                self.assertNotIn('"run", "-d"',self.fixture.with_suffix('.calls').read_text())
                del self.config['services']['api'][key]
        self.metadata['Cmd']=['node','--inspect=0.0.0.0:9229','dist/src/harness.js']
        self.assertNotEqual(self.candidate().returncode,0)

    def test_only_exact_readonly_fdb_volume_is_supported(self):
        mount={'type':'volume','source':'fdb-cluster-file','target':'/var/fdb','read_only':True,'volume':{}}
        self.config['services']['api']['volumes']=[mount]
        self.assertEqual(self.candidate().returncode,0)
        mount['read_only']=False
        self.assertNotEqual(self.candidate().returncode,0)
        self.assertEqual(json.loads((self.evidence/'checks.json').read_text())['status'],'blocked')

    def test_changed_runtime_config_rejects_deploy_manifest(self):
        self.assertEqual(self.candidate().returncode,0)
        self.config['services']['api']['environment']['EXTRA']='changed'
        self.assertNotEqual(self.manifest().returncode,0)
        self.assertFalse((self.evidence/'manifest.json').exists())

    def test_changed_guard_or_image_manifest_rejected(self):
        self.assertEqual(self.candidate().returncode,0)
        path=self.guard
        before=path.read_bytes(); path.write_bytes(before+b'\n# altered\n')
        self.assertNotEqual(self.manifest().returncode,0)
        path.write_bytes(before)
        (self.evidence/'images.json').write_text('{"services":{}}')
        self.assertNotEqual(self.manifest().returncode,0)

    def test_untrusted_or_modified_standalone_guard_cannot_admit_candidate(self):
        before=self.guard.read_bytes()
        self.guard.write_bytes(before+b'\n# modified\n')
        self.assertNotEqual(self.candidate().returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())
        self.guard.write_bytes(before); self.guard.chmod(0o644)
        self.assertNotEqual(self.candidate().returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())
        self.guard.unlink(); self.guard.symlink_to(GUARD)
        self.assertNotEqual(self.candidate().returncode,0)
        self.assertFalse((self.evidence/'images.json').exists())

    def test_runtime_policy_precedes_update_admission_and_build_is_before_switch(self):
        source=SOURCE.read_text()
        main=source[source.index('\nmain() {'):]
        cutover=source[source.index('\ncutover_candidate() {'):source.index('\nmain() {')]
        self.assertLess(main.index('  set_restart_policy\n'),main.index('  log "syncing'))
        self.assertLess(main.index('  prepare_candidate\n'),main.index('  gate receipt'))
        # Scheduled runs never switch source; only an approved cutover does.
        self.assertNotIn('git checkout',main)
        self.assertLess(main.index('gate approved'),main.index('  cutover_candidate\n'))
        self.assertLess(cutover.index('  gate consume'),cutover.index('  git checkout --detach'))
        self.assertLess(cutover.index('  verify_candidate_manifest\n'),cutover.index('  deployment_started=1'))
        self.assertNotIn('up --build',source)


@unittest.skipUnless(os.name == 'posix', 'Linux installer uses fcntl')
class RestartOverrideTests(unittest.TestCase):
    def test_user_override_is_preserved_and_wrong_render_fails_closed(self):
        from deployment import install_firecrawl_update as owner
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); target=root/'docker-compose.override.yaml'
            target.write_text('user-owned')
            with patch.object(owner,'REPO',root):
                with patch.object(owner.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'{"services":{}}','')):
                    with self.assertRaisesRegex(ValueError,'validation failed'): owner.install_restart_override()
                self.assertEqual(target.read_text(),'user-owned')
                target.unlink()
                result=subprocess.CompletedProcess([],0,'{"services":{}}','')
                with patch.object(owner.subprocess,'run',return_value=result):
                    with self.assertRaisesRegex(ValueError,'validation failed'): owner.install_restart_override()
                self.assertFalse(target.exists())
                config={'services':{name:{'restart':'unless-stopped'} for name in owner.SERVICES}}
                with patch.object(owner.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')):
                    owner.install_restart_override()
                self.assertEqual(target.read_bytes(),owner.RESTART)
                target.write_text('user-owned')
                with patch.object(owner.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')):
                    owner.install_restart_override()
                self.assertEqual(target.read_text(),'user-owned')


if __name__ == '__main__': unittest.main()
