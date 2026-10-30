import copy
import importlib.util
import hashlib
import ast
import unittest
import tempfile
from pathlib import Path
from unittest import mock

spec=importlib.util.spec_from_file_location('browser_upgrade',Path(__file__).with_name('upgrade-v629.py'))
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

class BrowserUpgradeContracts(unittest.TestCase):
    def test_snapshot_ignores_health_log_but_detects_health_and_mount_changes(self):
        row={'Id':'a','Image':'b','State':{'Running':True,'Health':{'Status':'healthy','Log':[1]}},'Mounts':[{'Destination':'/a','RW':True}]}
        snap={'services':{'api':row}}; original=u.logical_snapshot(snap)
        changed=copy.deepcopy(snap);changed['services']['api']['State']['Health']['Log']=[2]
        self.assertEqual(original,u.logical_snapshot(changed))
        changed['services']['api']['State']['Health']['Status']='unhealthy'
        self.assertNotEqual(original,u.logical_snapshot(changed))
        changed=copy.deepcopy(snap);changed['services']['api']['Mounts'][0]['RW']=False
        self.assertNotEqual(original,u.logical_snapshot(changed))
        for field,value in [('StartedAt','new'),('Restarting',True),('OOMKilled',True),('ExitCode',137)]:
            changed=copy.deepcopy(snap);changed['services']['api']['State'][field]=value
            self.assertNotEqual(original,u.logical_snapshot(changed),field)
        changed=copy.deepcopy(snap);changed['services']['api']['State']['Health']['FailingStreak']=1
        self.assertNotEqual(original,u.logical_snapshot(changed))
        with self.assertRaises(u.p.Stop):u.require_healthy(changed)
        u.require_healthy(snap)

    def test_lineage_and_env_require_exact_images(self):
        imgs={u.BASE_IMAGE:{'Id':u.BASE_IMAGE,'RootFS':{'Layers':['a','b']}},u.EXPECTED:{'Id':u.EXPECTED,'RootFS':{'Layers':['a','b','c']}}}
        u.verify_lineage(u.CURRENT_IMAGE,u.CURRENT_CORE,imgs.__getitem__)
        imgs[u.EXPECTED]['RootFS']['Layers']=['a','c']
        with self.assertRaises(u.p.Stop):u.verify_lineage(u.CURRENT_IMAGE,u.CURRENT_CORE,imgs.__getitem__)
        raw=('SECRET=a=b\nBASE_API='+u.CURRENT_BASE+'\nBROWSER_API_IMAGE='+u.CURRENT_IMAGE+'\n').encode()
        result=u.proposed_env(raw)
        self.assertIn(('BASE_API='+u.BASE_IMAGE).encode(),result)
        self.assertIn(('BROWSER_API_IMAGE='+u.EXPECTED).encode(),result)
        self.assertTrue(result.startswith(b'SECRET=a=b\n'))
        with self.assertRaises(u.p.Stop):u.proposed_env(raw.replace(u.CURRENT_BASE.encode(),b'old'))

    def test_running_core_requires_exact_config_rootfs_and_build_equivalence(self):
        images={u.BASE_IMAGE:{'Id':u.BASE_IMAGE,'RootFS':{'Type':'layers','Layers':['a','b']},'Config':{'Env':['X=1']}},
                u.CURRENT_CORE:{'Id':u.CURRENT_CORE,'RootFS':{'Type':'layers','Layers':['a','b']},'Config':{'Env':['X=1']}}}
        proof=u.verify_core_equivalence(images.__getitem__,lambda _:u.BUILD)
        self.assertEqual(proof['running_core'],u.CURRENT_CORE)
        self.assertEqual(proof['reviewed_base'],u.BASE_IMAGE)
        for field in ('Config','RootFS'):
            changed=copy.deepcopy(images);changed[u.CURRENT_CORE][field]={'different':True}
            with self.subTest(field=field),self.assertRaises(u.p.Stop):u.verify_core_equivalence(changed.__getitem__,lambda _:u.BUILD)
        with self.assertRaises(u.p.Stop):u.verify_core_equivalence(images.__getitem__,lambda image:u.BUILD if image==u.BASE_IMAGE else 'wrong')
        changed=copy.deepcopy(images);changed[u.CURRENT_CORE]['Id']='wrong'
        with self.assertRaises(u.p.Stop):u.verify_core_equivalence(changed.__getitem__,lambda _:u.BUILD)

    def test_only_reviewed_image_configuration_differences_allowed(self):
        labels={'org.firecrawl.patch.base':'base','org.firecrawl.patch.base-image-id':'id'}
        old={'Config':{'Env':['FIRECRAWL_BUILD_SHA=unknown','OTHER=x'],'Labels':labels}}
        new={'Config':{'Env':['FIRECRAWL_BUILD_SHA='+u.BUILD,'OTHER=x'],'Labels':{}}}
        before={'Config':copy.deepcopy(old['Config']),'HostConfig':{},'Mounts':[]}
        after={'Config':copy.deepcopy(new['Config']),'HostConfig':{},'Mounts':[]}
        u.verify_image_configuration(old,new)
        u.verify_current_image_configuration(before,old)
        u.verify_api_configuration(before,after,old,new)
        after['Config']['Env'].append('UNEXPECTED=x')
        with self.assertRaises(u.p.Stop):u.verify_api_configuration(before,after,old,new)
        after['Config']['Env'].pop();new['Config']['Labels']['unexpected']='x'
        with self.assertRaises(u.p.Stop):u.verify_api_configuration(before,after,old,new)
        with self.assertRaises(u.p.Stop):u.verify_image_configuration(old,new)
        new['Config']['Labels'].clear();new['Config']['Env'].append('UNEXPECTED=x')
        with self.assertRaises(u.p.Stop):u.verify_image_configuration(old,new)
        before['Config']['Env'][0]='FIRECRAWL_BUILD_SHA=wrong'
        with self.assertRaises(u.p.Stop):u.verify_current_image_configuration(before,old)

    def test_manifest_binds_all_sources_and_compiled_outputs(self):
        path=Path(__file__).with_name('candidate-v629.manifest.json')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        import json
        data=json.loads(path.read_text());compiled={**data['compiled'],**data['base_compiled']}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'deployment/self-hosted-browser';base.mkdir(parents=True)
            for name in data['sources']:
                source=Path(__file__).resolve().parents[2]/name
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(source.read_bytes().replace(b'\r\n',b'\n'))
            fixture=base/'candidate-v629.manifest.json';fixture.write_bytes(path.read_bytes())
            with mock.patch.object(u,'BASE',base), mock.patch.object(u.p,'run',side_effect=lambda args,timeout: compiled[args[-1]]+'  '+args[-1]):
                self.assertEqual(u.checked_manifest(fixture,digest),digest)
                with self.assertRaises(u.p.Stop):u.checked_manifest(fixture,'0'*64)

    def test_failed_candidate_stop_checks_name_project_service_and_image(self):
        row={'Id':'id','Name':'/firecrawl-browser-api-1','Image':u.EXPECTED,'State':{'Running':True},
             'Config':{'Labels':{'com.docker.compose.project':'firecrawl-browser','com.docker.compose.service':'api'}}}
        stopped=copy.deepcopy(row);stopped['State']['Running']=False
        with mock.patch.object(u.p,'run',side_effect=['id','']) as run, mock.patch.object(u.p,'inspect',side_effect=[row,stopped]):
            self.assertEqual(u.stop_failed_candidate(),'stopped')
            self.assertEqual(run.call_args_list[1].args[0],['docker','stop','--time','60','id'])
        for key in ('Name','Image','project','service'):
            bad=copy.deepcopy(row)
            if key in ('Name','Image'):bad[key]='wrong'
            else:bad['Config']['Labels']['com.docker.compose.'+key]='wrong'
            with self.subTest(key=key),mock.patch.object(u.p,'run',return_value='id') as run, mock.patch.object(u.p,'inspect',return_value=bad):
                with self.assertRaises(u.p.Stop):u.stop_failed_candidate()
                self.assertEqual(run.call_count,1)
        with mock.patch.object(u.p,'run',return_value=''):
            self.assertEqual(u.stop_failed_candidate(),'not-present')

    def test_live_http_connection_rejects_cutover(self):
        with mock.patch.object(u.p,'run',return_value='ESTAB fixture'):
            with self.assertRaises(u.p.Stop):u.require_no_browser_connections()
        with mock.patch.object(u.p,'run',return_value=''):
            u.require_no_browser_connections()

    def test_canonical_updater_and_guard_pins_and_audit_extraction(self):
        updater=Path(__file__).resolve().parents[1]/'firecrawl-updater/deployment/firecrawl_update.sh'
        guard=updater.with_name('firecrawl_transport_guard.py')
        self.assertEqual(hashlib.sha256(updater.read_bytes()).hexdigest(),u.UPDATER_SHA)
        self.assertEqual(hashlib.sha256(guard.read_bytes()).hexdigest(),u.GUARD_SHA)
        script=updater.read_text(encoding='utf-8')
        candidate=script.split("<<'PYCANDIDATE'\n",1)[1].split('\nPYCANDIDATE',1)[0]
        tree=ast.parse(candidate)
        values=[n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign)
                and any(isinstance(t,ast.Name) and t.id=='package_audit_source' for t in n.targets)
                and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)]
        self.assertEqual(len(values),1)

if __name__=='__main__':unittest.main()
