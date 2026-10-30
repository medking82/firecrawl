import copy
import importlib.util
import hashlib
import ast
import unittest
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
        u.verify_lineage(u.CURRENT_IMAGE,u.BASE_IMAGE,imgs.__getitem__)
        imgs[u.EXPECTED]['RootFS']['Layers']=['a','c']
        with self.assertRaises(u.p.Stop):u.verify_lineage(u.CURRENT_IMAGE,u.BASE_IMAGE,imgs.__getitem__)
        raw=('SECRET=a=b\nBASE_API='+u.CURRENT_BASE+'\nBROWSER_API_IMAGE='+u.CURRENT_IMAGE+'\n').encode()
        result=u.proposed_env(raw)
        self.assertIn(('BASE_API='+u.BASE_IMAGE).encode(),result)
        self.assertIn(('BROWSER_API_IMAGE='+u.EXPECTED).encode(),result)
        self.assertTrue(result.startswith(b'SECRET=a=b\n'))
        with self.assertRaises(u.p.Stop):u.proposed_env(raw.replace(u.CURRENT_BASE.encode(),b'old'))

    def test_only_reviewed_image_configuration_differences_allowed(self):
        labels={'org.firecrawl.patch.base':'base','org.firecrawl.patch.base-image-id':'id'}
        old={'Config':{'Env':['FIRECRAWL_BUILD_SHA=unknown','OTHER=x'],'Labels':labels}}
        new={'Config':{'Env':['FIRECRAWL_BUILD_SHA='+u.BUILD,'OTHER=x'],'Labels':{}}}
        before={'Config':copy.deepcopy(old['Config']),'HostConfig':{},'Mounts':[]}
        after={'Config':copy.deepcopy(new['Config']),'HostConfig':{},'Mounts':[]}
        u.verify_api_configuration(before,after,old,new)
        after['Config']['Env'].append('UNEXPECTED=x')
        with self.assertRaises(u.p.Stop):u.verify_api_configuration(before,after,old,new)
        after['Config']['Env'].pop();new['Config']['Labels']['unexpected']='x'
        with self.assertRaises(u.p.Stop):u.verify_api_configuration(before,after,old,new)

    def test_manifest_binds_all_sources_and_compiled_outputs(self):
        path=Path(__file__).with_name('candidate-v629.manifest.json')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        import json
        data=json.loads(path.read_text());compiled={**data['compiled'],**data['base_compiled']}
        with mock.patch.object(u.p,'run',side_effect=lambda args,timeout: compiled[args[-1]]+'  '+args[-1]):
            self.assertEqual(u.checked_manifest(path,digest),digest)
            with self.assertRaises(u.p.Stop):u.checked_manifest(path,'0'*64)

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
