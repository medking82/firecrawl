"""Real Docker/Compose crash recovery fixture. Never selects production projects."""
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import uuid
import security_refresh as s

BASE = 'sha256:978f0e01593e65eed801f2402944efcd936d43b5027e4908a7897baf88ed6241'


def main():
    token = uuid.uuid4().hex[:12]
    root = Path(tempfile.mkdtemp(prefix='fc-refresh-fixture-', dir='/tmp'))
    projects = tuple('fc-refresh-fixture-' + token + '-' + x for x in ('a','b','c'))
    p = s.helper(); p.BROWSER = root
    s.PROJECTS = projects
    s.ready = lambda p, project: s.run(['docker','exec',s.identity(p,project)[0],'redis-cli','ping'])
    tag = 'fc-refresh-fixture:' + token
    candidate = None
    config = root/'compose.json'
    try:
        s.run(['docker','tag',BASE,tag])
        (root/'Dockerfile').write_text('FROM '+tag+'\nRUN echo '+token+' > /tmp/fixture\n')
        env = dict(os.environ, DOCKER_BUILDKIT='0')
        import subprocess
        result = subprocess.run(['docker','build','--network=none','--pull=false','--iidfile',str(root/'iid'),str(root)],env=env,capture_output=True,timeout=120)
        if result.returncode: raise RuntimeError('fixture build failed')
        candidate = (root/'iid').read_text().strip()
        s.save(config, {'services':{'api':{'image':BASE,'network_mode':'none','read_only':True,
                     'tmpfs':['/tmp'],'command':['redis-server','--save','','--appendonly','no'],
                     'labels':{'firecrawl.security-fixture':token}}}})
        for project in projects:
            p.compose(project,[str(config)],str(root),None,'up','-d','--no-build','--pull','never','api')
        for point in ('before-stop','stopped','removed','recreated','after-ready'):
            directory=root/point;directory.mkdir(mode=0o700)
            snaps={k:p.project_snapshot(k) for k in projects}
            files={str(config):s.hashlib.sha256(config.read_bytes()).hexdigest()}
            for snap in snaps.values():
                for path in snap['paths']: files[path]=s.hashlib.sha256(Path(path).read_bytes()).hexdigest()
            record={'status':'applying','snapshots':snaps,'old':{k:BASE for k in projects},
                    'new':{k:candidate for k in projects},'files':files}
            journal=directory/'transaction.json';s.save(journal,record)
            s.save(root/'admission-paused',{'owner':str(journal)})
            pid=os.fork()
            if pid==0:
                try:
                    if point!='before-stop': s.run(['docker','stop','--time','5',s.identity(p,projects[0])[0]])
                    if point=='removed': s.run(['docker','rm',snaps[projects[0]]['services']['api']['Id']])
                    if point in ('recreated','after-ready'):
                        s.apply_image(p,record,projects[0],candidate,directory,'candidate')
                    if point=='after-ready':
                        for project in projects[1:]: s.apply_image(p,record,project,candidate,directory,'candidate')
                    os.kill(os.getpid(),signal.SIGKILL)
                except Exception: os._exit(3)
            _,status=os.waitpid(pid,0)
            if not os.WIFSIGNALED(status) or os.WTERMSIG(status)!=signal.SIGKILL:
                raise RuntimeError('fixture child did not reach crash point: '+point)
            s.recover(p,journal)
            s.recover(p,journal)  # Repeated boot recovery must be harmless.
            assert json.loads(journal.read_text())['status']=='rolled-back'
            assert not (root/'admission-paused').exists()
            for project in projects:
                assert s.identity(p,project)[1]==BASE
                assert s.ready(p,project)=='PONG'
            print('PASS SIGKILL recovery: '+point,flush=True)
        print('PASS real Compose rollback, missing-container recovery, idempotence, gate release')
    finally:
        for project in projects:
            try: p.compose(project,[str(config)],str(root),None,'down','--remove-orphans')
            except Exception: pass
        for image in (candidate,tag):
            if image:
                try: s.run(['docker','image','rm',image])
                except Exception: pass
        if root.parent==Path('/tmp') and root.name.startswith('fc-refresh-fixture-'):
            shutil.rmtree(root)


if __name__=='__main__':main()
