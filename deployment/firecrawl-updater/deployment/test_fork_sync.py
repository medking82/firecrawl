"""Real Git integration tests; every remote and worktree is disposable."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SYNC = Path(__file__).with_name('firecrawl_update.sh').resolve()


def run(*args, cwd=None, check=True, env=None):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=check)


class ForkSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.origin = self.root / 'origin.git'; self.upstream = self.root / 'upstream.git'
        self.seed = self.root / 'seed'; self.repo = self.root / 'runtime'; self.state = self.root / 'state'
        self.state.mkdir()
        run('git','init','--bare','--initial-branch=main',str(self.origin))
        run('git','init','--bare','--initial-branch=main',str(self.upstream))
        run('git','init','--initial-branch=main',str(self.seed))
        self.git('config','user.name','Fixture'); self.git('config','user.email','fixture@example.invalid')
        self.write('shared.txt','base\n'); self.commit('base')
        self.git('remote','add','origin',str(self.origin)); self.git('remote','add','upstream',str(self.upstream))
        self.git('push','origin','main'); self.git('push','upstream','main')
        run('git','clone',str(self.origin),str(self.repo))
        run('git','remote','add','upstream',str(self.upstream),cwd=self.repo)
        run('git','checkout','--detach',cwd=self.repo)
        self.initial = self.ref(self.origin)

    def git(self,*args): return run('git',*args,cwd=self.seed).stdout.strip()
    def write(self,name,text): (self.seed/name).write_text(text,encoding='utf-8')
    def commit(self,message): self.git('add','--all'); self.git('commit','-m',message)
    def ref(self,remote): return run('git','--git-dir',str(remote),'rev-parse','refs/heads/main').stdout.strip()
    def invoke(self, moved_upstream=False):
        env=os.environ.copy(); env.update({'FIXTURE_STATE':str(self.state),'FIXTURE_SYNC':str(SYNC), 'FIRECRAWL_UPDATE_STATE_DIR':str(self.state), 'GIT_TERMINAL_PROMPT':'0'})
        override = "git() { if [[ $1 == ls-remote && $3 == upstream ]]; then printf '%040d\\trefs/heads/main\\n' 1; else command git \"$@\"; fi; }; " if moved_upstream else ''
        command='set -Eeuo pipefail; state_dir=$FIXTURE_STATE; source "$FIXTURE_SYNC"; '+override+'sync_fork_main'
        return run('bash','-c',command,cwd=self.repo,env=env,check=False)
    def fork_and_upstream(self, conflict=False):
        self.write('shared.txt' if conflict else 'fork.txt','fork change\n'); self.commit('fork')
        fork=self.git('rev-parse','HEAD'); self.git('push','origin','main')
        self.git('reset','--hard',self.initial)
        self.write('shared.txt' if conflict else 'upstream.txt','upstream change\n'); self.commit('upstream')
        upstream=self.git('rev-parse','HEAD'); self.git('push','upstream','main')
        return fork,upstream

    def test_diverged_merge_preserves_both_histories_and_checkout(self):
        fork,upstream=self.fork_and_upstream()
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr)
        merged=self.ref(self.origin)
        self.assertEqual(run('git','--git-dir',str(self.origin),'rev-parse',merged+'^1').stdout.strip(),fork)
        self.assertEqual(run('git','--git-dir',str(self.origin),'rev-parse',merged+'^2').stdout.strip(),upstream)
        self.assertEqual(run('git','rev-parse','HEAD',cwd=self.repo).stdout.strip(),self.initial)
        evidence=next(self.state.glob('fork-sync.*/snapshots')); self.assertIn('status=complete',evidence.read_text())
        self.assertFalse((evidence.parent/'worktree').exists())

    def test_conflict_preserves_evidence_and_never_pushes(self):
        fork,_=self.fork_and_upstream(conflict=True)
        result=self.invoke(); self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.ref(self.origin),fork)
        evidence=next(self.state.glob('fork-sync.*/snapshots'))
        self.assertTrue((evidence.parent/'worktree').exists())
        self.assertNotIn('status=complete',evidence.read_text())

    def test_fast_forward_and_already_current(self):
        self.write('upstream.txt','new\n');self.commit('upstream');self.git('push','upstream','main')
        wanted=self.ref(self.upstream)
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.ref(self.origin),wanted)
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.ref(self.origin),wanted)

    def test_rejected_push_preserves_origin_and_evidence(self):
        fork,_=self.fork_and_upstream()
        hook=self.origin/'hooks'/'pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
        result=self.invoke();self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.ref(self.origin),fork)
        self.assertTrue((next(self.state.glob('fork-sync.*'))/'worktree').exists())

    def test_moved_upstream_stops_before_push(self):
        fork,_=self.fork_and_upstream()
        result=self.invoke(moved_upstream=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.ref(self.origin),fork)
        self.assertTrue((next(self.state.glob('fork-sync.*'))/'worktree').exists())


if __name__ == '__main__': unittest.main()
