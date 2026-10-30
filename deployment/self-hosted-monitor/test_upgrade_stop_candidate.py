import copy
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from upgrade_stop_candidate import stop_candidate
from test_upgrade_v629 import u

ID = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64


class Stop(Exception): pass


def row():
    return {'Id': ID, 'Image': IMAGE, 'State': {'Running': True},
            'Config': {'Labels': {'com.docker.compose.project': 'firecrawl-monitor',
                                  'com.docker.compose.service': 'api'}}}


class CandidateStopTests(unittest.TestCase):
    def test_exact_candidate_is_stopped_and_verified(self):
        before = row(); after = copy.deepcopy(before); after['State']['Running'] = False
        owner = SimpleNamespace(Stop=Stop, run=mock.Mock(side_effect=[ID, '']), inspect=mock.Mock(side_effect=[before, after]))
        self.assertEqual(stop_candidate(owner, IMAGE), 'candidate-stopped')
        owner.run.assert_any_call(['docker', 'stop', '--time', '60', ID], timeout=90)

    def test_no_running_api_does_not_issue_stop(self):
        owner = SimpleNamespace(Stop=Stop, run=mock.Mock(return_value=''), inspect=mock.Mock())
        self.assertEqual(stop_candidate(owner, IMAGE), 'no-running-api')
        owner.inspect.assert_not_called(); self.assertEqual(owner.run.call_count, 1)

    def test_ambiguous_and_foreign_are_not_stopped(self):
        for listing, changed in [(ID+'\n'+'b'*64, None), ('short', None), (ID, {'Image':'other'}),
                                 (ID, {'Config':{'Labels':{'com.docker.compose.project':'other','com.docker.compose.service':'api'}}})]:
            with self.subTest(listing=listing, changed=changed):
                item = row(); item.update(changed or {})
                owner = SimpleNamespace(Stop=Stop, run=mock.Mock(return_value=listing), inspect=mock.Mock(return_value=item))
                with self.assertRaises(Stop): stop_candidate(owner, IMAGE)
                self.assertEqual(owner.run.call_count, 1)

    def test_failed_stop_verification_is_failure(self):
        owner = SimpleNamespace(Stop=Stop, run=mock.Mock(side_effect=[ID, '']), inspect=mock.Mock(side_effect=[row(), row()]))
        with self.assertRaises(Stop): stop_candidate(owner, IMAGE)

    def test_owner_postflight_failure_stops_candidate_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); updater = root / 'state'; updater.mkdir()
            env = root / 'runtime.env'
            env.write_text('BASE_API='+u.CURRENT_BASE_REF+'\nMONITOR_IMAGE='+u.CURRENT_IMAGE+'\nSECRET=fixture\n')
            config = root / 'compose.yaml'; config.write_text('fixture')
            def row(name, image):
                return {'Id': name, 'Image': image, 'State': {'Running': True},
                        'Config': {'Labels': {}}, 'HostConfig': {}, 'Mounts': []}
            api = row('old-api', u.CURRENT_IMAGE); db = row('db', 'db-image')
            snap = {'project':u.PROJECT, 'paths':[str(config)], 'workdir':str(root), 'envfile':str(env),
                    'services':{'api':api, 'app-db':db}}
            others = {'firecrawl': {'services': {'api': row('core',u.BASE_IMAGE)}},
                      'firecrawl-browser': {'services': {'api': row('browser','browser-image')}}}
            candidate = row(ID, u.EXPECTED)
            candidate['Config']['Labels'] = {'com.docker.compose.project':u.PROJECT,'com.docker.compose.service':'api'}
            running = [True]; recreated = [False]
            def snapshots(name):
                if name != u.PROJECT: return copy.deepcopy(others[name])
                result = copy.deepcopy(snap)
                if recreated[0]: result['services']['api'] = copy.deepcopy(candidate)
                return result
            def inspect(kind, ref):
                if kind == 'image': return {'Id':ref}
                if ref == 'old-api': return {'State':{'Running':False}}
                result = copy.deepcopy(candidate); result['State']['Running'] = running[0]; return result
            def run(args, **kwargs):
                if args[:3] == ['docker','ps','-q']: return ID
                if args[:2] == ['docker','stop'] and args[-1] == ID: running[0] = False
                return ''
            def render(snapshot, envfile):
                data = u.p.env_value(Path(envfile).read_bytes(),'MONITOR_IMAGE')
                return {'services':{'api':{'image':data},'app-db':{'image':'db-image'}}}
            def compose(*args): recreated[0] = True
            patches = [mock.patch.object(u.p,'UPDATER',updater), mock.patch.object(u,'ENV',env),
                mock.patch.object(u.p,'locks',lambda:contextlib.nullcontext()),mock.patch.object(u,'verify_backup',return_value={}),
                mock.patch.object(u.p,'project_snapshot',side_effect=snapshots),mock.patch.object(u.p,'inspect',side_effect=inspect),
                mock.patch.object(u.p,'render',side_effect=render),mock.patch.object(u.p,'run',side_effect=run),
                mock.patch.object(u.p,'compose',side_effect=compose),mock.patch.object(u,'verify_image_binding'),
                mock.patch.object(u,'verify_image_configuration',return_value=({},{})),mock.patch.object(u,'verify_current_image_configuration'),
                mock.patch.object(u,'stable_summary',side_effect=lambda x:x),mock.patch.object(u,'installed_package_audit',return_value={}),
                mock.patch.object(u,'idle',return_value=True),mock.patch.object(u,'http_idle',return_value=True),
                mock.patch.object(u,'check_schema'),mock.patch.object(u,'verify_api_configuration'),
                mock.patch.object(u,'wait_ready',side_effect=u.p.Stop('fixture readiness failed')),
                mock.patch.object(u.time,'sleep'),mock.patch.object(u.os,'fchmod',create=True),mock.patch.object(u.os,'fchown',create=True)]
            with contextlib.ExitStack() as stack:
                for patch in patches: stack.enter_context(patch)
                with self.assertRaises(u.p.Stop):
                    u.upgrade('/fixture',3600,str(updater/'guards'/(u.GUARD_SHA+'.py')),u.GUARD_SHA)
            receipt = json.loads(next(updater.glob('monitor-v629-*/receipt.json')).read_text())
            self.assertEqual(receipt['candidate_cleanup'],'candidate-stopped')
            self.assertEqual(receipt['status'],'failed-manual-intervention')
            self.assertFalse(running[0])


if __name__ == '__main__': unittest.main()
