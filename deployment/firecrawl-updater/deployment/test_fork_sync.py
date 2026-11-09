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
    def invoke(self, moved_upstream=False, check_state='success', timeout='0'):
        fake_bin = self.root / 'bin'; fake_bin.mkdir(exist_ok=True)
        gh = fake_bin / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
state = Path(os.environ['FIXTURE_STATE'])
args = sys.argv[1:]
with (state / 'gh-args').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
sha = args[-1].split('/commits/')[1].split('/')[0]
remote = os.environ['FIXTURE_ORIGIN']
refs = subprocess.check_output(['git', '--git-dir', remote, 'for-each-ref', '--format=%(objectname)', 'refs/heads/updater/'], text=True).split()
if sha not in refs:
    raise SystemExit('check requested before candidate publication')
mode = os.environ['FIXTURE_CHECK_STATE']
if mode == 'api_error': raise SystemExit(1)
checks = [{'id': 1, 'name': 'LF policy', 'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'app': {'slug': 'github-actions'}}]
if mode == 'pending': checks[0].update(status='in_progress', conclusion=None)
if mode == 'failure': checks[0]['conclusion'] = 'failure'
if mode == 'wrong_sha': checks[0]['head_sha'] = '0' * 40
if mode == 'missing': checks = []
if mode == 'wrong_name': checks[0]['name'] = 'Other policy'
if mode == 'wrong_app': checks[0]['app']['slug'] = 'other-app'
if mode in ('cancelled', 'neutral', 'skipped'): checks[0]['conclusion'] = mode
if mode == 'pending_then_success' and not (state / 'pending-seen').exists():
    (state / 'pending-seen').touch()
    checks[0].update(status='in_progress', conclusion=None)
if mode == 'malformed':
    print('not json'); raise SystemExit(0)
if mode == 'origin_moved':
    subprocess.check_call(['git', '--git-dir', remote, 'update-ref', 'refs/heads/main', sha])
if mode == 'upstream_moved':
    subprocess.check_call(['git', '--git-dir', os.environ['FIXTURE_UPSTREAM'], 'update-ref', 'refs/heads/main', os.environ['FIXTURE_INITIAL']])
if checks and checks[0]['status'] == 'completed' and checks[0]['conclusion'] == 'success':
    (state / 'checked-sha').write_text(sha)
print(json.dumps([{'check_runs': checks}]))
''', encoding='utf-8')
        gh.chmod(0o755)
        env=os.environ.copy(); env.update({'FIXTURE_STATE':str(self.state),'FIXTURE_SYNC':str(SYNC), 'FIRECRAWL_UPDATE_STATE_DIR':str(self.state), 'GIT_TERMINAL_PROMPT':'0',
            'PATH':str(fake_bin)+os.pathsep+env['PATH'], 'FIXTURE_ORIGIN':str(self.origin), 'FIXTURE_CHECK_STATE':check_state,
            'FIRECRAWL_SYNC_CHECK_TIMEOUT':timeout, 'FIRECRAWL_SYNC_REPOSITORY':'fixture/firecrawl',
            'FIXTURE_UPSTREAM':str(self.upstream), 'FIXTURE_INITIAL':self.initial})
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

    def test_failed_check_keeps_main_and_allows_unattended_retry(self):
        fork, _ = self.fork_and_upstream()
        result = self.invoke(check_state='failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ref(self.origin), fork)
        self.assertTrue((self.state / 'gh-args').is_file())
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.ref(self.origin), fork)

    def test_only_successful_lf_check_for_exact_sha_allows_main(self):
        fork, _ = self.fork_and_upstream()
        for mode in ('pending', 'missing', 'wrong_sha', 'wrong_name', 'wrong_app',
                     'cancelled', 'neutral', 'skipped', 'api_error', 'malformed'):
            with self.subTest(mode=mode):
                result = self.invoke(check_state=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.ref(self.origin), fork)
                self.assertFalse(any(self.state.glob('fork-sync.*/worktree')))

    def test_pending_check_is_polled_until_success(self):
        self.fork_and_upstream()
        result = self.invoke(check_state='pending_then_success', timeout='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.state / 'gh-args').read_text().splitlines()), 2)
        self.assertEqual((self.state / 'checked-sha').read_text(), self.ref(self.origin))

    def test_main_push_requires_previously_checked_candidate_sha(self):
        self.fork_and_upstream()
        hook = self.origin / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nwhile read old new ref; do\n'
                        '  if [ "$ref" = refs/heads/main ]; then\n'
                        f'    test "$(cat "{self.state / "checked-sha"}")" = "$new" || exit 1\n'
                        '  fi\ndone\n', encoding='utf-8')
        hook.chmod(0o755)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'checked-sha').read_text(), self.ref(self.origin))

    def assert_remote_movement_blocks_push(self, mode):
        fork, _ = self.fork_and_upstream()
        result = self.invoke(check_state=mode)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('remote moved during LF check', result.stdout)
        if mode == 'upstream_moved':
            self.assertEqual(self.ref(self.origin), fork)
        self.assertTrue(any(self.state.glob('fork-sync.*/worktree')))

    def test_moved_origin_during_check_stops_before_main_push(self):
        self.assert_remote_movement_blocks_push('origin_moved')

    def test_moved_upstream_during_check_stops_before_main_push(self):
        self.assert_remote_movement_blocks_push('upstream_moved')

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
        again=self.invoke(); self.assertNotEqual(again.returncode,0)
        self.assertIn('prior fork sync requires inspection',again.stdout)
        self.assertEqual(len(list(self.state.glob('fork-sync.*'))),1)
        self.assertEqual(self.ref(self.origin),fork)

    def test_upstream_trailing_whitespace_does_not_block_sync(self):
        self.write('upstream.txt','upstream content  \n'); self.commit('upstream whitespace')
        self.git('push','upstream','main'); wanted=self.ref(self.upstream)
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.ref(self.origin),wanted)

    def test_incomplete_worktree_without_snapshots_blocks_new_worktree(self):
        previous=self.state/'fork-sync.previous'; (previous/'worktree').mkdir(parents=True)
        result=self.invoke(); self.assertNotEqual(result.returncode,0)
        self.assertEqual(list(self.state.glob('fork-sync.*')),[previous])

    def test_fast_forward_and_already_current(self):
        self.write('upstream.txt','new\n');self.commit('upstream');self.git('push','upstream','main')
        wanted=self.ref(self.upstream)
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.ref(self.origin),wanted)
        result=self.invoke(); self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.ref(self.origin),wanted)

    def test_rejected_push_preserves_origin_and_evidence(self):
        fork,_=self.fork_and_upstream()
        hook=self.origin/'hooks'/'pre-receive';hook.write_text('#!/bin/sh\nwhile read old new ref; do\n  [ "$ref" != refs/heads/main ] || exit 1\ndone\n');hook.chmod(0o755)
        result=self.invoke();self.assertNotEqual(result.returncode,0)
        self.assertTrue((self.state / 'gh-args').exists())
        self.assertEqual(self.ref(self.origin),fork)
        self.assertTrue((next(self.state.glob('fork-sync.*'))/'worktree').exists())

    def test_moved_upstream_stops_before_push(self):
        fork,_=self.fork_and_upstream()
        result=self.invoke(moved_upstream=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.ref(self.origin),fork)
        self.assertTrue((next(self.state.glob('fork-sync.*'))/'worktree').exists())


if __name__ == '__main__': unittest.main()
