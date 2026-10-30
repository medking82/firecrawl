"""Browser v2.11.629 upgrade owner. Execute only after independent review."""
import ast
import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / 'patched-base'))
import promote as p
from recovery_backup import verify_backup

EXPECTED = 'sha256:e97dde38a74efbbbcc487a4d99e90a12bab8f8000c4c7759d6cbeabc1e9a409d'
BASE_IMAGE = 'sha256:b967b93629d2a1c2a46ab160058f29f5d23ae67e6e8b3880656ec4d0f8d78cfd'
CURRENT_CORE = 'sha256:ea19422d7a62e0a7421f56e52878aaa023c8c829f0470bfdc85acf15865988b6'
CURRENT_IMAGE = 'sha256:f7df127568145a769bbb5c37933c218d9be5b59697b7ad7311829dc9ea6909a4'
CURRENT_BASE = 'sha256:394360309d3cfc655d1cc4802f2886c3e779da90637f6dbe99b2859461de2071'
BUILD = '74f81da6935bcbf581480a888c2ed3fd81fc2d85'
ENV = p.BROWSER / 'runtime.env'
SCHEMA = BASE / '003-agent-sponsors.sql'
GUARD_SHA='07f73956c9531c483ccd9816af9b3aa06de87424dc0ab33758e9daa2c5e16c3d'
UPDATER_SHA='b853b7a642a24238b0f9aa540263a9343d87e490e5e609a7271f29b3f918a664'
TRANSPORT_SHA='558cb5d6354dcdbf7394e70d0f8130e9e43ee5c0fb312cf4b55dd6504f3d4f69'

def logical_snapshot(snapshot):
    """Ignore ephemeral health probe logs, but detect identity and logical state drift."""
    result = {}
    for name, row in snapshot['services'].items():
        state = row['State']
        health=state.get('Health') or {}
        result[name] = {'id':row['Id'], 'image':row['Image'], 'running':state['Running'],
                        'started_at':state.get('StartedAt'), 'restarting':state.get('Restarting'),
                        'oom_killed':state.get('OOMKilled'), 'exit_code':state.get('ExitCode'),
                        'health':health.get('Status'), 'health_failing_streak':health.get('FailingStreak'),
                        'mounts':sorted([{k:m.get(k) for k in ('Type','Name','Source','Destination','RW')}
                                         for m in row.get('Mounts',[])], key=lambda m:m['Destination'])}
    return result

def require_healthy(snapshot):
    for name,row in logical_snapshot(snapshot).items():
        if (row['running'] is not True or row['restarting'] or row['oom_killed'] or row['exit_code'] not in (0,None)
            or row['health'] not in (None,'healthy') or row['health_failing_streak'] not in (None,0)):
            raise p.Stop('container is not healthy: '+name)

def verify_lineage(current, core, image_lookup):
    if current != CURRENT_IMAGE or core != CURRENT_CORE: raise p.Stop('running image preimage changed')
    base = image_lookup(BASE_IMAGE); candidate = image_lookup(EXPECTED)
    if base.get('Id') != BASE_IMAGE or candidate.get('Id') != EXPECTED: raise p.Stop('image ID mismatch')
    a = base.get('RootFS',{}).get('Layers'); b = candidate.get('RootFS',{}).get('Layers')
    if not isinstance(a,list) or not a or not isinstance(b,list) or len(b)<=len(a) or b[:len(a)]!=a:
        raise p.Stop('browser candidate does not extend the reviewed core')

def verify_core_equivalence(image_lookup, build_lookup):
    """Accept only the two pinned images with identical runtime configuration and bytes."""
    base=image_lookup(BASE_IMAGE); current=image_lookup(CURRENT_CORE)
    if base.get('Id')!=BASE_IMAGE or current.get('Id')!=CURRENT_CORE:
        raise p.Stop('core equivalence image identity changed')
    root=base.get('RootFS');config=base.get('Config')
    if not isinstance(root,dict) or not root.get('Layers') or root!=current.get('RootFS'):
        raise p.Stop('core root filesystem differs from reviewed base')
    if not isinstance(config,dict) or config!=current.get('Config'):
        raise p.Stop('core configuration differs from reviewed base')
    if build_lookup(BASE_IMAGE)!=BUILD or build_lookup(CURRENT_CORE)!=BUILD:
        raise p.Stop('core build revision differs from reviewed base')
    digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'reviewed_base':BASE_IMAGE,'running_core':CURRENT_CORE,'build_sha':BUILD,
            'rootfs_sha256':digest(root),'config_sha256':digest(config)}

def image_build(image):
    return p.run(['docker','run','--rm','--pull=never','--network','none','--read-only',
                  '--label','com.docker.compose.project=browser-core-audit',
                  '--entrypoint','cat',image,'/app/BUILD_SHA'],timeout=30)

def env_map(config):
    out={}
    for item in config.get('Env') or []:
        k,sep,v=item.partition('=')
        if not sep or k in out: raise p.Stop('duplicate or invalid environment')
        out[k]=v
    return out

def verify_image_configuration(old_image, new_image):
    old, new = old_image['Config'],new_image['Config']
    a,b = env_map(old),env_map(new)
    changes={k for k in a.keys() | b.keys() if a.get(k)!=b.get(k)}
    if changes!={'FIRECRAWL_BUILD_SHA'} or a.get('FIRECRAWL_BUILD_SHA')!='unknown' or b.get('FIRECRAWL_BUILD_SHA')!=BUILD:
        raise p.Stop('unreviewed image environment difference')
    al,bl=old.get('Labels') or {},new.get('Labels') or {}
    allowed={'org.firecrawl.patch.base','org.firecrawl.patch.base-image-id'}
    if {k for k in al.keys() | bl.keys() if al.get(k)!=bl.get(k)}!=allowed or any(k in bl for k in allowed):
        raise p.Stop('unreviewed image label difference')
    return a,b,al,bl,allowed

def verify_current_image_configuration(before, old_image):
    expected=env_map(old_image['Config'])
    actual=env_map(before['Config'])
    if actual.get('FIRECRAWL_BUILD_SHA')!=expected.get('FIRECRAWL_BUILD_SHA'):
        raise p.Stop('current container build SHA differs from image')
    labels=old_image['Config'].get('Labels') or {}
    for key in ('org.firecrawl.patch.base','org.firecrawl.patch.base-image-id'):
        if (before['Config'].get('Labels') or {}).get(key)!=labels.get(key):
            raise p.Stop('current container image label differs from image')

def verify_api_configuration(before, after, old_image, new_image):
    a,b,al,bl,allowed=verify_image_configuration(old_image,new_image)
    verify_current_image_configuration(before,old_image)
    left,right=p.api_configuration(before),p.api_configuration(after)
    ae,be=env_map(left['config']),env_map(right['config'])
    if ae.get('FIRECRAWL_BUILD_SHA')!=a['FIRECRAWL_BUILD_SHA'] or be.get('FIRECRAWL_BUILD_SHA')!=b['FIRECRAWL_BUILD_SHA']:
        raise p.Stop('container build SHA differs from image')
    for entry in (left,right): entry['config']['Env']=[x for x in entry['config']['Env'] if not x.startswith('FIRECRAWL_BUILD_SHA=')]
    for key in allowed:
        if (before['Config'].get('Labels') or {}).get(key)!=al.get(key) or (after['Config'].get('Labels') or {}).get(key)!=bl.get(key):
            raise p.Stop('container label differs from image')
    if left!=right: raise p.Stop('browser API configuration changed')

def proposed_env(raw):
    if p.env_value(raw,'BASE_API')!=CURRENT_BASE or p.env_value(raw,'BROWSER_API_IMAGE')!=CURRENT_IMAGE:
        raise p.Stop('browser runtime image references changed')
    return p.replace_env(raw,{'BASE_API':BASE_IMAGE,'BROWSER_API_IMAGE':EXPECTED})

def preflight(backup, max_age):
    """Read-only snapshot. No locks, gate, files, services or environment are changed."""
    backup_evidence=verify_backup(backup,max_age)
    browser=p.project_snapshot('firecrawl-browser'); core=p.project_snapshot('firecrawl')
    if browser.get('envfile') and Path(browser['envfile']) != ENV: raise p.Stop('unexpected browser env owner')
    current=browser['services']['api']; core_api=core['services']['api']
    verify_lineage(current['Image'],core_api['Image'],lambda ref:p.inspect('image',ref))
    core_equivalence=verify_core_equivalence(lambda ref:p.inspect('image',ref),image_build)
    old_image=p.inspect('image',CURRENT_IMAGE);new_image=p.inspect('image',EXPECTED)
    verify_image_configuration(old_image,new_image);verify_current_image_configuration(current,old_image)
    raw=ENV.read_bytes(); updated=proposed_env(raw)
    return {'status':'prepared-not-admitted','backup':backup_evidence,'candidate':EXPECTED,
            'base':BASE_IMAGE,'build':BUILD,'core_equivalence':core_equivalence,'current':logical_snapshot(browser),
            'runtime_env_sha256':hashlib.sha256(raw).hexdigest(),
            'proposed_env_sha256':hashlib.sha256(updated).hexdigest()}

def verify_manifest(path, expected_sha):
    """Require reviewed source and image-byte bindings, not mutable tag labels."""
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise p.Stop('reviewed manifest hash mismatch')
    data=json.loads(raw)
    if data.get('candidate')!=EXPECTED or data.get('base')!=BASE_IMAGE or data.get('build')!=BUILD:
        raise p.Stop('manifest image or revision mismatch')
    sources=data.get('sources'); compiled=data.get('compiled'); base_compiled=data.get('base_compiled')
    required={'lib/local-artifacts','lib/gcs-jobs','lib/gcs-monitoring','lib/local-monitor-judge',
              'services/monitoring/judgeChange','services/logging/log_job','lib/local-hangar','lib/hangar',
              'lib/local-browser-agent','lib/scrape-interact/browser-agent','controllers/v2/browser',
              'lib/local-browser-job-store','lib/job-access-store','lib/job-state-store'}
    source_required={'apps/api/src/'+x+'.ts' for x in required} | {
        'deployment/self-hosted-browser/hangar-compat.cjs','deployment/self-hosted-browser/compile.cjs',
        'deployment/self-hosted-browser/deploy.py','deployment/self-hosted-browser/003-agent-sponsors.sql'}
    if not isinstance(sources,dict) or not isinstance(compiled,dict) or not isinstance(base_compiled,dict) or set(sources)!=source_required or set(compiled)!={'/app/dist/src/'+x+'.js' for x in required}:
        raise p.Stop('manifest lacks source or compiled bindings')
    if set(base_compiled)!={'/app/dist/src/lib/browser-options.js','/app/dist/src/lib/browser-lifecycle.js',
        '/app/dist/src/lib/browser-sessions.js','/app/dist/src/lib/scrape-interact/scrape-replay.js',
        '/app/dist/src/controllers/v2/types.js','/app/dist/src/routes/shared.js',
        '/app/dist/src/services/billing/credit_billing.js','/app/dist/src/services/billing/agent-mcp-credits.js'}:
        raise p.Stop('manifest lacks reviewed upstream modules')
    root=BASE.parents[1]
    for rel,digest in sources.items():
        target=(root/rel).resolve(strict=True)
        if not target.is_relative_to(root.resolve()) or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
            raise p.Stop('reviewed source differs from manifest')
    for rel,digest in compiled.items():
        if not rel.startswith('/app/dist/src/') or '..' in Path(rel).parts or not rel.endswith('.js'):
            raise p.Stop('invalid compiled manifest path')
        actual=p.run(['docker','run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL',
                      '--entrypoint','sha256sum',EXPECTED,rel],timeout=30).split()[0]
        if actual!=digest: raise p.Stop('compiled image bytes differ from manifest')
    for rel,digest in base_compiled.items():
        for image in (BASE_IMAGE,EXPECTED):
            actual=p.run(['docker','run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL',
                          '--entrypoint','sha256sum',image,rel],timeout=30).split()[0]
            if actual!=digest: raise p.Stop('upstream module differs from reviewed base')
    return hashlib.sha256(raw).hexdigest()

def checked_manifest(path, expected_sha):
    verified=verify_manifest(path,expected_sha)
    if Path(path).read_bytes()!= (BASE/'candidate-v629.manifest.json').read_bytes():
        raise p.Stop('manifest differs from packaged candidate manifest')
    return verified

def package_audit():
    raw=Path('/home/marck/.local/bin/firecrawl-update').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=UPDATER_SHA: raise p.Stop('installed updater identity changed')
    parts=raw.decode().split("<<'PYCANDIDATE'\n",1)
    if len(parts)!=2: raise p.Stop('installed package audit missing')
    tree=ast.parse(parts[1].split('\nPYCANDIDATE',1)[0])
    values=[n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='package_audit_source' for t in n.targets)
            and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)]
    if len(values)!=1: raise p.Stop('installed package audit ambiguous')
    result=json.loads(p.run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
        '--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges','--memory','256m','--cpus','1',
        '--pids-limit','32','--user','0','--entrypoint','node',EXPECTED,'-e',values[0],'/'],timeout=240))
    if set(result)!={'decode-uri-component','systeminformation'} or any(not v for v in result.values()):
        raise p.Stop('package audit invalid')
    return result

def idle(db):
    queue="SELECT (SELECT count(*) FROM nuq.queue_scrape WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_crawl_finished WHERE status IN ('queued','active')) + (SELECT count(*) FROM nuq.queue_scrape_backlog)"
    activity="SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND state='active'"
    def sql(database,query):
        return p.run(['docker','exec',db,'psql','-X','-U','postgres','-d',database,'-v','ON_ERROR_STOP=1','-At','-c',query],timeout=30)
    return sql('postgres',queue)=='0' and sql('postgres',activity)=='0' and sql('browser',activity)=='0' and not p.active_sessions()

def wait_ready():
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        try:
            if p.readiness(3007): return
        except Exception: pass
        time.sleep(2)
    raise p.Stop('browser readiness timeout')

def stop_failed_candidate():
    """Stop only the exact browser API candidate; ambiguous state stays gated."""
    name='firecrawl-browser-api-1'
    ids=p.run(['docker','ps','-aq','--no-trunc','--filter','name=^/'+name+'$'],timeout=15).split()
    if not ids:return 'not-present'
    if len(ids)!=1:raise p.Stop('ambiguous browser API container')
    row=p.inspect('container',ids[0]);labels=row.get('Config',{}).get('Labels') or {}
    if (row.get('Name')!='/'+name or row.get('Id')!=ids[0] or row.get('Image')!=EXPECTED or
        labels.get('com.docker.compose.project')!='firecrawl-browser' or
        labels.get('com.docker.compose.service')!='api'):
        raise p.Stop('candidate identity cannot be verified; gate retained')
    if row['State']['Running']:
        p.run(['docker','stop','--time','60',row['Id']],timeout=90)
        if p.inspect('container',row['Id'])['State']['Running']:
            raise p.Stop('candidate API did not stop; gate retained')
        return 'stopped'
    return 'already-stopped'

def require_no_browser_connections():
    if p.run(['ss','-H','-tn','state','established','( sport = :3007 )'],timeout=10):
        raise p.Stop('browser has active connections')

def upgrade(backup,max_age,manifest,manifest_sha,guard,guard_sha):
    os.umask(0o077)
    with p.locks():
        backup_evidence=verify_backup(backup,max_age)
        snapshot=p.project_snapshot('firecrawl-browser'); core=p.project_snapshot('firecrawl')
        others={name:p.project_snapshot(name) for name in ('firecrawl','firecrawl-monitor')}
        require_healthy(snapshot)
        for item in others.values():require_healthy(item)
        current=snapshot['services']['api']; db=snapshot['services']['app-db']
        if not db['State']['Running']: raise p.Stop('browser database is not running')
        if snapshot.get('envfile') and Path(snapshot['envfile'])!=ENV: raise p.Stop('unexpected browser environment owner')
        verify_lineage(current['Image'],core['services']['api']['Image'],lambda ref:p.inspect('image',ref))
        core_equivalence=verify_core_equivalence(lambda ref:p.inspect('image',ref),image_build)
        old_image=p.inspect('image',CURRENT_IMAGE);new_image=p.inspect('image',EXPECTED)
        verify_image_configuration(old_image,new_image);verify_current_image_configuration(current,old_image)
        source_digest=checked_manifest(manifest,manifest_sha)
        raw=ENV.read_bytes(); st=ENV.stat(); updated=proposed_env(raw)
        if hasattr(os,'listxattr') and os.listxattr(ENV): raise p.Stop('unsupported runtime.env attributes')
        config=p.render(snapshot,envfile=ENV)
        with tempfile.TemporaryDirectory(dir=p.UPDATER) as tmp:
            staged=Path(tmp)/'runtime.env';staged.write_bytes(updated);staged.chmod(0o600)
            proposed=p.render(snapshot,envfile=staged)
            if p.without_images(config)!=p.without_images(proposed): raise p.Stop('non-image compose changed')
            for name,service in config['services'].items():
                if proposed['services'][name].get('image') != (EXPECTED if name=='api' else service.get('image')):
                    raise p.Stop('unexpected compose image change')
        if guard_sha!=GUARD_SHA or Path(guard)!=p.UPDATER/'guards'/(GUARD_SHA+'.py'): raise p.Stop('unexpected transport guard')
        transport=BASE.parent/'patched-base'/'verify-transport.py'
        if hashlib.sha256(transport.read_bytes()).hexdigest()!=TRANSPORT_SHA: raise p.Stop('transport verifier changed')
        p.run([sys.executable,'-B',str(transport),EXPECTED,guard,guard_sha],timeout=240)
        packages=package_audit()
        schema=SCHEMA.read_text(encoding='utf-8'); schema_sha=hashlib.sha256(schema.encode()).hexdigest()
        files={x:hashlib.sha256(Path(x).read_bytes()).hexdigest() for x in snapshot['paths']}
        manager_unit=Path('/home/marck/.config/systemd/user/firecrawl-browser-manager.service')
        llm_unit=Path('/home/marck/.config/systemd/user/firecrawl-browser-llm.service')
        unit_hashes={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in (manager_unit,llm_unit)}
        if p.run(['systemctl','--user','is-active','firecrawl-browser-llm.service'])!='active':raise p.Stop('browser LLM bridge is not active')
        before=logical_snapshot(snapshot);other_before={k:logical_snapshot(v) for k,v in others.items()}
        receipt_dir=p.UPDATER/('browser-v629-'+uuid.uuid4().hex);receipt_dir.mkdir(mode=0o700)
        p.atomic_write(receipt_dir/'runtime.env.before',raw)
        record={'status':'preflight','backup':backup_evidence,'before':before,'candidate':EXPECTED,'base':BASE_IMAGE,
                'manifest_sha256':source_digest,'schema_sha256':schema_sha,'package_audit':packages,'core_equivalence':core_equivalence,
                'env_sha256':hashlib.sha256(raw).hexdigest(),'compose_sha256':files,'unit_sha256':unit_hashes}
        def save():p.atomic_write(receipt_dir/'receipt.json',json.dumps(record,indent=2).encode())
        save();gate=p.BROWSER/'admission-paused'; frozen=False; paused=False; success=False; watchdog=None
        try:
            if not idle(db['Id']):raise p.Stop('browser is busy')
            require_no_browser_connections()
            pid=p.manager_pid()
            with gate.open('x') as f:f.write(receipt_dir.name)
            paused=True;record['status']='admission-paused';save()
            if p.manager_pid()!=pid:raise p.Stop('manager identity changed')
            watchdog=p.start_thaw_watchdog(pid);frozen=True;p.freeze(pid)
            if not idle(db['Id']) or p.manager_pid()!=pid:raise p.Stop('browser became busy')
            require_no_browser_connections()
            if logical_snapshot(p.project_snapshot('firecrawl-browser'))!=before or ENV.read_bytes()!=raw:
                raise p.Stop('browser changed during preflight')
            if any(hashlib.sha256(Path(k).read_bytes()).hexdigest()!=v for k,v in unit_hashes.items()):raise p.Stop('browser unit changed')
            if any(hashlib.sha256(Path(k).read_bytes()).hexdigest()!=v for k,v in files.items()):raise p.Stop('compose source changed')
            if checked_manifest(manifest,manifest_sha)!=source_digest:raise p.Stop('source binding changed')
            if (p.project_snapshot('firecrawl')['services']['api']['Image']!=CURRENT_CORE or
                verify_core_equivalence(lambda ref:p.inspect('image',ref),image_build)!=core_equivalence):
                raise p.Stop('core equivalence changed during preflight')
            verify_image_configuration(p.inspect('image',CURRENT_IMAGE),p.inspect('image',EXPECTED))
            verify_current_image_configuration(current,p.inspect('image',CURRENT_IMAGE))
            record['status']='stopping-api';save();p.run(['docker','stop','--time','60',current['Id']],timeout=90)
            if p.inspect('container',current['Id'])['State']['Running'] or not idle(db['Id']):
                p.run(['docker','start',current['Id']],timeout=90);wait_ready();raise p.Stop('busy after stop; original API restarted')
            if ENV.read_bytes()!=raw or ENV.stat()!=st or SCHEMA.read_text(encoding='utf-8')!=schema:
                raise p.Stop('environment or schema changed')
            record['status']='applying-schema';save()
            result=subprocess.run(['docker','exec','-i',db['Id'],'psql','-X','-U','postgres','-d','browser','-v','ON_ERROR_STOP=1'],
                                  input=schema,text=True,capture_output=True,timeout=60)
            if result.returncode:raise p.Stop('browser schema migration failed')
            record['status']='updating-env';save();p.atomic_write(ENV,updated,st.st_mode & 0o777,(st.st_uid,st.st_gid))
            if p.render(snapshot,envfile=ENV)!=proposed:raise p.Stop('rendered compose changed')
            record['status']='recreating-api';save()
            p.compose('firecrawl-browser',snapshot['paths'],snapshot['workdir'],ENV,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate','api')
            after=p.project_snapshot('firecrawl-browser')
            if after['services']['api']['Image']!=EXPECTED:raise p.Stop('candidate image mismatch')
            require_healthy(after)
            old_nonapi=copy.deepcopy(before);old_nonapi.pop('api');new_nonapi=logical_snapshot(after);new_nonapi.pop('api')
            if old_nonapi!=new_nonapi:raise p.Stop('browser non-API services changed')
            verify_api_configuration(current,after['services']['api'],p.inspect('image',CURRENT_IMAGE),p.inspect('image',EXPECTED))
            wait_ready()
            for name in other_before:
                if logical_snapshot(p.project_snapshot(name))!=other_before[name]:raise p.Stop('other project changed')
            if p.manager_pid()!=pid or not idle(db['Id']) or ENV.read_bytes()!=updated:raise p.Stop('postflight drift')
            if any(hashlib.sha256(Path(k).read_bytes()).hexdigest()!=v for k,v in unit_hashes.items()):raise p.Stop('browser unit changed')
            record['status']='ready-admission-paused';record['after']=logical_snapshot(after);save();success=True
        except Exception as exc:
            record['failure_stage']=record['status'];record['status']='failed-manual-intervention'
            record['failure']=str(exc) if isinstance(exc,p.Stop) else type(exc).__name__
            if record['failure_stage'] in ('recreating-api','ready-admission-paused'):
                try:record['candidate_stop']=stop_failed_candidate()
                except Exception as stop_error:
                    record['candidate_stop']='failed'
                    record['candidate_stop_error']=str(stop_error) if isinstance(stop_error,p.Stop) else type(stop_error).__name__
            save();raise
        finally:
            try:
                if frozen:
                    try:os.kill(pid,signal.SIGCONT)
                    except ProcessLookupError:raise p.Stop('manager disappeared; gate retained')
                if watchdog:watchdog.stdin.close();watchdog.wait(timeout=5)
                if success and paused:
                    if gate.read_text()!=receipt_dir.name:raise p.Stop('admission gate owner changed')
                    gate.unlink()
            except Exception as final_error:
                if success:
                    record['failure_stage']='finalizing';record['status']='failed-manual-intervention'
                    record['failure']=str(final_error) if isinstance(final_error,p.Stop) else type(final_error).__name__
                    try:record['candidate_stop']=stop_failed_candidate()
                    except Exception as stop_error:
                        record['candidate_stop']='failed'
                        record['candidate_stop_error']=str(stop_error) if isinstance(stop_error,p.Stop) else type(stop_error).__name__
                    save()
                raise
        record['status']='complete';save();return receipt_dir/'receipt.json'

if __name__ == '__main__':
    import argparse
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(p.Stop('upgrade interrupted')))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup',required=True);parser.add_argument('--max-backup-age-seconds',type=int,required=True)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--manifest');parser.add_argument('--manifest-sha256');parser.add_argument('--guard');parser.add_argument('--guard-sha256')
    args=parser.parse_args()
    if args.execute:
        if not all((args.manifest,args.manifest_sha256,args.guard,args.guard_sha256)):parser.error('--execute requires manifest and guard identity')
        print(json.dumps({'receipt':str(upgrade(args.backup,args.max_backup_age_seconds,args.manifest,args.manifest_sha256,args.guard,args.guard_sha256))}))
    else:print(json.dumps(preflight(args.backup,args.max_backup_age_seconds),indent=2))
