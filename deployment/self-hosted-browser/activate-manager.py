"""Activate a reviewed manager-only release while idle; never rebuild/restart the API stack."""
import argparse,fcntl,hashlib,http.client,json,os,signal,subprocess,time
from pathlib import Path

ROOT=Path('/home/marck/services/firecrawl-browser')
STATE=ROOT/'shared'
HERE=Path(__file__).resolve().parent
RELEASE=HERE.parents[1]
UNIT=Path('/home/marck/.config/systemd/user/firecrawl-browser-manager.service')
NAME='firecrawl-browser-manager.service'

def output(args):return subprocess.check_output(args,text=True,timeout=5).strip()
def run(args):subprocess.run(args,check=True,capture_output=True,text=True,timeout=30)
def private(path,text):
    temp=path.with_suffix('.tmp');temp.write_text(text);temp.chmod(0o600);temp.replace(path)
def active():
    rows=int(output(['docker','exec','firecrawl-browser-app-db-1','psql','-X','-U','postgres','-d','browser','-At','-c',"SELECT count(*) FROM browser_sessions WHERE status='active'"]))
    live=output(['docker','ps','-aq','--filter','label=firecrawl.self-hosted-browser=session'])
    saved=json.loads((STATE/'sessions.json').read_text()) if (STATE/'sessions.json').exists() else {'sessions':[]}
    directories=any((STATE/'sessions').glob('br_*'))
    return rows or bool(live) or directories or any(s['status'] in ('starting','running') for s in saved['sessions'])
def preserved():
    containers={}
    for name in output(['docker','ps','--format','{{.Names}}']).splitlines():
        if name.startswith('firecrawl-') and not name.startswith('firecrawl-browser-session-'):
            row=json.loads(output(['docker','inspect',name]))[0]
            containers[name]={'image':row['Image'],'started':row['State']['StartedAt'],'running':row['State']['Running']}
    paths=[Path('/home/marck/github/firecrawl')/n for n in ['.env','docker-compose.yaml','docker-compose.override.yaml']]
    paths += [Path('/home/marck/services')/n/'shared/runtime.env' for n in ['firecrawl-monitor','firecrawl-browser']]
    return {'containers':containers,'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            'other_units':{n:output(['systemctl','--user','show',n,'--property=ActiveState,MainPID,ExecMainStartTimestamp']) for n in ['firecrawl-browser-llm.service','firecrawl-monitor-judge.service']}}
def ready():
    if output(['systemctl','--user','is-active',NAME])!='active':return False
    conn=http.client.HTTPConnection('127.0.0.1',3008,timeout=2)
    try:
        conn.request('GET','/');response=conn.getresponse();response.read();return response.status==404
    finally:conn.close()

def freeze(pid):
    """Bootstrap old managers which do not yet understand the admission marker."""
    if pid<=1:raise RuntimeError('invalid_manager_pid')
    os.kill(pid,signal.SIGSTOP)
    for _ in range(100):
        if '\nState:\tT' in Path(f'/proc/{pid}/status').read_text():return
        time.sleep(0.01)
    raise RuntimeError('manager_freeze_failed')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--expected-manager-sha256',required=True)
    args=parser.parse_args();os.umask(0o077)
    if RELEASE.parent!=ROOT/'manager-releases':raise RuntimeError('immutable_manager_release_required')
    manifest=json.loads((RELEASE/'manager-manifest.json').read_text())
    for name,sha in manifest['files'].items():
        file=(RELEASE/name).resolve()
        if not file.is_relative_to(RELEASE) or hashlib.sha256(file.read_bytes()).hexdigest()!=sha:raise RuntimeError('manager_release_integrity_failed')
    with (STATE/'manager-activation.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        original=UNIT.read_text();old=next(line for line in original.splitlines() if line.startswith('ExecStart=/usr/bin/node '))
        new='ExecStart=/usr/bin/node '+str(HERE/'manager.cjs');already=old==new
        current=Path(old.removeprefix('ExecStart=/usr/bin/node '))
        if not already and hashlib.sha256(current.read_bytes()).hexdigest()!=args.expected_manager_sha256:raise RuntimeError('manager_changed_since_review')
        before=preserved();old_pid=output(['systemctl','--user','show',NAME,'--value','--property=MainPID'])
        if not already:
            # Defer before blocking admission if existing work is visible.
            if active():raise SystemExit('browser_manager_busy_defer_activation')
            gate=STATE/'admission-paused'
            # Never replace another operator's admission/drain gate.
            with gate.open('x') as marker:marker.write(RELEASE.name)
            frozen=False;killed=False
            try:
                if output(['systemctl','--user','show',NAME,'--value','--property=MainPID'])!=old_pid:raise RuntimeError('manager_pid_changed')
                # A stopped event loop cannot admit from existing keep-alive
                # connections either. Recheck persisted/in-flight resources only
                # after the old manager is frozen, including pending directories.
                frozen=True;freeze(int(old_pid))
                if active():raise RuntimeError('browser_manager_busy_defer_activation')
                private(STATE/('manager-unit-before-'+RELEASE.name+'.service'),original)
                private(UNIT,original.replace(old,new,1))
                run(['systemctl','--user','daemon-reload'])
                # SIGTERM/SIGCONT could resume queued admissions in the old code.
                # There are no sessions; end this frozen idle process atomically.
                os.kill(int(old_pid),signal.SIGKILL);killed=True;frozen=False
                run(['systemctl','--user','restart',NAME])
                for _ in range(20):
                    try:
                        if ready():break
                    except Exception:pass
                    time.sleep(0.5)
                else:raise RuntimeError('manager_readiness_failed')
            except Exception:
                if killed and not active():
                    private(UNIT,original);run(['systemctl','--user','daemon-reload']);run(['systemctl','--user','restart',NAME])
                elif not killed and UNIT.read_text()!=original:
                    private(UNIT,original);run(['systemctl','--user','daemon-reload'])
                raise
            finally:
                if frozen:
                    try:os.kill(int(old_pid),signal.SIGCONT)
                    except ProcessLookupError:pass
                if gate.read_text()==RELEASE.name:gate.unlink()
        elif not ready():raise RuntimeError('activated_manager_not_ready')
        after=preserved()
        if before!=after:raise RuntimeError('existing_service_changed')
        data=json.loads((STATE/'deployment.json').read_text())
        data.update(manager_release=RELEASE.name,manager_source_commit=manifest['source_commit'],
                    manager_source_sha256=manifest['files']['deployment/self-hosted-browser/manager.cjs'])
        private(STATE/'deployment.json',json.dumps(data,indent=2))
        receipt={'manager_release':RELEASE.name,'source_commit':manifest['source_commit'],'api_release':data['release'],
                 'previous_manager_pid':old_pid,'manager_pid':output(['systemctl','--user','show',NAME,'--value','--property=MainPID']),
                 'already_active':already,'before':before,'after':after,'existing_services_unchanged':True}
        private(STATE/'manager-activation.json',json.dumps(receipt,indent=2));print(json.dumps(receipt))
