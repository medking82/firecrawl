"""Drive the real approval gate and main control flow on a fixture, never production."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).with_name('firecrawl_update.sh')
API = 'sha256:' + 'a' * 64
BASE = 'sha256:' + 'b' * 64

STUBS = r'''
set_restart_policy() { :; }
validate_config() { :; }
verify_candidate_manifest() { :; }
smoke_test() {
  if [[ -e "$FIXTURE/smoke-fail-once" ]]; then rm "$FIXTURE/smoke-fail-once"; return 1; fi
}
snapshot_images() {
  [[ ! -e "$FIXTURE/snapshot-fails" ]] || return 1
  preimage=$(mktemp -d "$state_dir/preimage.XXXXXXXX")
}
restore_images() { echo restore >>"$FIXTURE/calls"; }
sync_fork_main() { git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main; }
prepare_candidate() {
  echo "prepare $latest_tag" >>"$FIXTURE/calls"
  candidate_dir="$worktree_root/update-$latest_commit"
  [[ -e "$candidate_dir" ]] || git worktree add --detach "$candidate_dir" "$latest_commit" >/dev/null 2>&1
  candidate_evidence=$(mktemp -d "$state_dir/candidate.XXXXXXXX")
  python3 -B - "$candidate_evidence" "$latest_tag" "$latest_commit" "$FIXTURE" <<'PY'
import hashlib, json, sys
from pathlib import Path
evidence, tag, commit, fixture = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
status = (fixture / 'admission').read_text().strip() if (fixture / 'admission').exists() else 'checks_passed'
config = json.loads((fixture / 'config.json').read_text())
for service in config['services'].values(): service.pop('build', None)
images = {'api': {'image': 'sha256:' + 'a' * 64, 'pull_policy': 'never'},
          'nuq-postgres': {'image': 'sha256:' + 'd' * 64, 'pull_policy': 'never'}}
report = {'schema': 1, 'status': status, 'tag': tag, 'source_commit': commit}
if status == 'checks_passed':
    report.update(images=images, guard_sha256='0' * 64, review_basis={'profile': 'v2.11.629'},
                  runtime_config_sha256=hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                  package_check={'api_image': images['api']['image'], 'packages': {'systeminformation': [{'version': '5.33.18'}]}})
    (evidence / 'images.json').write_text(json.dumps({'services': images}, sort_keys=True))
(evidence / 'patched-iid').write_text('sha256:' + 'a' * 64)
(evidence / 'checks.json').write_text(json.dumps(report, sort_keys=True))
PY
  candidate_base_image=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  [[ "$(cat "$FIXTURE/admission" 2>/dev/null || echo checks_passed)" == checks_passed ]]
  candidate_checks_sha256=$(sha256sum "$candidate_evidence/checks.json")
  candidate_checks_sha256=${candidate_checks_sha256%% *}
}
'''

DOCKER = '''import json, os, sys
from pathlib import Path
fixture = Path(os.environ['FIXTURE']); args = sys.argv[1:]
with (fixture / 'docker-calls').open('a') as stream: stream.write(json.dumps(args) + '\\n')
marker = lambda image: fixture / 'images' / image.replace(':', '_')
if 'config' in args: print((fixture / 'config.json').read_text())
elif args[:2] == ['image', 'inspect']: sys.exit(0 if marker(args[2]).exists() else 1)
elif args[:2] == ['image', 'rm']: marker(args[2]).unlink()
'''

GH = '''import json, os, sys
from pathlib import Path
fixture = Path(os.environ['FIXTURE']); args = sys.argv[1:]
if (fixture / 'gh-fails').exists(): sys.exit(1)
method = args[args.index('--method') + 1]; path = args[args.index('-H') + 2]
store_path = fixture / 'issues.json'
store = json.loads(store_path.read_text()) if store_path.exists() else {'next': 1, 'issues': {}}
payload = json.loads(sys.stdin.read()) if '--input' in args else None
parts = path.split('?')[0].split('/')
issues = store['issues']
if method == 'GET' and len(parts) == 4:
    out = [dict(v, number=int(k)) for k, v in issues.items() if v['state'] == 'open']
elif method == 'GET':
    out = dict(issues[parts[4]], number=int(parts[4]))
elif method == 'POST' and len(parts) == 4:
    number = str(store['next']); store['next'] += 1
    issues[number] = dict(payload, state='open', comments=[]); out = {'number': int(number)}
elif method == 'PATCH':
    issues[parts[4]].update(payload); out = {'number': int(parts[4])}
else:
    issues[parts[4]]['comments'].append(payload['body']); out = {}
store_path.write_text(json.dumps(store))
print(json.dumps(out))
'''


def embedded(marker):
    text = SOURCE.read_text()
    return text.split("<<'" + marker + "'\n", 1)[1].split('\n' + marker, 1)[0]


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('flock'), 'GA401 Bash owner required')
class ApprovalFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.fixture = root / 'fixture'; (self.fixture / 'images').mkdir(parents=True)
        self.state = root / 'state'; self.state.mkdir(mode=0o700)
        self.worktrees = root / 'worktrees'; self.worktrees.mkdir()
        self.security = root / 'security'; self.security.mkdir()
        self.bin = root / 'bin'; self.bin.mkdir()
        for name, body in (('docker', DOCKER), ('gh', GH)):
            path = self.bin / name
            path.write_text('#!' + sys.executable + '\n' + body); path.chmod(0o700)
        refresh = root / 'security_refresh.py'
        refresh.write_text('from pathlib import Path\nimport os\n'
                           'Path(os.environ["FIXTURE"], "security-refreshed").touch()\n')
        (self.fixture / 'config.json').write_text(json.dumps({'services': {'api': {'build': {'context': '.'}, 'environment': {'A': '1'}}}}))
        git_env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        self.git_env = git_env
        self.upstream, self.origin, self.work, self.repo = root / 'upstream.git', root / 'origin.git', root / 'work', root / 'repo'
        for bare in (self.upstream, self.origin):
            self.git('init', '--bare', '-b', 'main', str(bare))
        self.git('init', '-b', 'main', str(self.work))
        self.release('v2.11.1')
        self.git('clone', '-q', str(self.origin), str(self.repo))
        self.git('-C', str(self.repo), 'remote', 'add', 'upstream', str(self.upstream))
        self.git('-C', str(self.repo), 'fetch', '-q', '--tags', 'upstream')
        self.git('-C', str(self.repo), 'checkout', '-q', '--detach', 'v2.11.1')
        (self.repo / '.env').write_text('A=1\n')
        self.env = dict(git_env, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], FIXTURE=str(self.fixture),
                        FIRECRAWL_REPO=str(self.repo), FIRECRAWL_UPDATE_STATE_DIR=str(self.state),
                        FIRECRAWL_WORKTREE_ROOT=str(self.worktrees), FIRECRAWL_SECURITY_REFRESH=str(refresh),
                        FIRECRAWL_SECURITY_STATE=str(self.security), FIRECRAWL_NOTICE_REPO='owner/alerts',
                        FIRECRAWL_NOTICE_GH=str(self.bin / 'gh'))

    def git(self, *args):
        subprocess.run(['git', *args], check=True, capture_output=True, env=self.git_env)

    def release(self, tag):
        (self.work / 'VERSION').write_text(tag)
        self.git('-C', str(self.work), 'add', 'VERSION')
        self.git('-C', str(self.work), 'commit', '-q', '-m', tag)
        self.git('-C', str(self.work), 'tag', tag)
        for remote in (self.upstream, self.origin):
            self.git('-C', str(self.work), 'push', '-q', str(remote), 'main', tag)

    def run_main(self, *args):
        script = 'source "$1"; shift\n' + STUBS + 'main "$@"\n'
        return subprocess.run(['bash', '-c', script, 'fixture', str(SOURCE), *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        path = self.fixture / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def issues(self):
        return json.loads((self.fixture / 'issues.json').read_text())['issues']

    def composed_up(self):
        path = self.fixture / 'docker-calls'
        return [row for row in map(json.loads, path.read_text().splitlines()) if 'up' in row] if path.exists() else []

    def head(self):
        return subprocess.run(['git', '-C', str(self.repo), 'describe', '--tags', '--exact-match'],
                              capture_output=True, text=True).stdout.strip()

    def test_candidate_waits_for_exact_approval_then_cuts_over_once(self):
        self.release('v2.11.2')
        result = self.run_main(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ['prepare v2.11.2'])
        receipt = json.loads((self.state / 'candidate-ready.json').read_text())
        self.assertEqual((receipt['tag'], receipt['api_image'], receipt['base_image']), ('v2.11.2', API, BASE))
        issue = self.issues()['1']
        self.assertEqual(issue['assignees'], ['medking82'])
        self.assertIn('firecrawl-upgrade --approve v2.11.2', issue['body'])
        self.assertIn('/compare/v2.11.1...v2.11.2', issue['body'])
        self.assertEqual(self.composed_up(), [])
        self.assertEqual(self.head(), 'v2.11.1')
        # Unapproved: later runs neither rebuild, cut over nor repeat the notice.
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.calls(), ['prepare v2.11.2'])
        self.assertEqual(self.issues()['1']['comments'], [])
        self.assertEqual(self.composed_up(), [])
        wrong = self.run_main('--approve', 'v2.11.1')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('not v2.11.1', wrong.stderr)
        self.assertFalse((self.state / 'approval.json').exists())
        result = self.run_main('--approve', 'v2.11.2'); self.assertEqual(result.returncode, 0, result.stderr)
        approval = json.loads((self.state / 'approval.json').read_text())
        self.assertEqual((approval['tag'], approval['commit']), ('v2.11.2', receipt['commit']))
        self.assertEqual(approval['checks_sha256'], receipt['checks_sha256'])
        result = self.run_main(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(), 'v2.11.2')
        self.assertEqual(len(self.composed_up()), 1)
        self.assertIn(receipt['evidence'] + '/images.json', self.composed_up()[0])
        self.assertFalse((self.state / 'approval.json').exists())
        self.assertEqual(len(list((self.state / 'approvals').glob('consumed-*.json'))), 1)
        self.assertFalse((self.state / 'candidate-ready.json').exists())
        self.assertIn('tag=v2.11.2', (self.state / 'deployed.env').read_text())
        self.assertTrue((self.fixture / 'security-refreshed').exists())
        issue = self.issues()['1']
        self.assertEqual(issue['state'], 'closed')
        self.assertIn('security refresh passed', issue['comments'][-1])
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(len(self.composed_up()), 1)

    def test_newer_tag_invalidates_approval_and_retains_superseded_candidate(self):
        self.release('v2.11.2')
        self.assertEqual(self.run_main().returncode, 0)
        old = json.loads((self.state / 'candidate-ready.json').read_text())
        self.assertTrue(Path(old['candidate_dir']).is_dir())
        self.assertEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)
        for image in (API, BASE):
            (self.fixture / 'images' / image.replace(':', '_')).touch()
        self.release('v2.11.3')
        result = self.run_main(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.composed_up(), [])
        self.assertEqual(self.head(), 'v2.11.1')
        self.assertFalse((self.state / 'approval.json').exists())
        self.assertEqual(len(list((self.state / 'approvals').glob('superseded-*.json'))), 1)
        self.assertEqual(json.loads((self.state / 'candidate-ready.json').read_text())['tag'], 'v2.11.3')
        record = json.loads((self.state / 'candidates' / (Path(old['evidence']).name + '.json')).read_text())
        self.assertEqual(record['status'], 'superseded')
        self.assertTrue(record['cleaned'], record)
        self.assertFalse(Path(old['candidate_dir']).exists())
        self.assertFalse(any((self.fixture / 'images').iterdir()))
        issue = self.issues()['1']
        self.assertIn('v2.11.3', issue['body'])
        self.assertIn('v2.11.3', issue['comments'][-1])
        # The old tag can no longer be approved.
        self.assertNotEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)

    def test_failed_cutover_rolls_back_and_never_reuses_the_approval(self):
        self.release('v2.11.2')
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)
        (self.fixture / 'smoke-fail-once').touch()
        result = self.run_main(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('restore', self.calls())
        self.assertEqual(self.head(), 'v2.11.1')
        self.assertFalse((self.state / 'approval.json').exists())
        self.assertFalse((self.state / 'candidate-ready.json').exists())
        self.assertFalse((self.fixture / 'security-refreshed').exists())
        issue = self.issues()['1']
        self.assertEqual(issue['state'], 'open')
        self.assertIn('previous version restored', issue['comments'][-1])
        # A new candidate and a new approval are required.
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.calls().count('prepare v2.11.2'), 2)
        self.assertEqual(len(self.composed_up()), 1)

    def test_precheck_failure_keeps_approval_without_changes(self):
        self.release('v2.11.2')
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)
        (self.fixture / 'snapshot-fails').touch()
        self.assertNotEqual(self.run_main().returncode, 0)
        self.assertTrue((self.state / 'approval.json').exists())
        self.assertEqual(self.head(), 'v2.11.1')
        self.assertEqual(self.composed_up(), [])
        self.assertIn('approval kept', self.issues()['1']['comments'][-1])

    def test_runtime_configuration_change_withdraws_approval_and_rebuilds(self):
        self.release('v2.11.2')
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)
        (self.fixture / 'config.json').write_text(json.dumps({'services': {'api': {'environment': {'A': '2'}}}}))
        result = self.run_main(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.composed_up(), [])
        self.assertFalse((self.state / 'approval.json').exists())
        self.assertEqual(len(list((self.state / 'approvals').glob('stale-*.json'))), 1)
        self.assertEqual(self.calls().count('prepare v2.11.2'), 2)
        self.assertTrue((self.state / 'candidate-ready.json').exists())

    def test_failed_admission_notifies_once_and_waits_for_review(self):
        self.release('v2.11.2')
        (self.fixture / 'admission').write_text('awaiting_transport_review')
        self.assertNotEqual(self.run_main().returncode, 0)
        self.assertFalse((self.state / 'candidate-ready.json').exists())
        issue = self.issues()['1']
        self.assertIn('awaiting_transport_review', issue['body'])
        self.assertNotIn('--approve', issue['body'])
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.calls(), ['prepare v2.11.2'])
        self.assertNotEqual(self.run_main('--approve', 'v2.11.2').returncode, 0)
        # A changed runtime configuration (or guard) earns a fresh attempt.
        (self.repo / '.env').write_text('A=2\n')
        (self.fixture / 'admission').unlink()
        self.assertEqual(self.run_main().returncode, 0)
        self.assertEqual(self.calls().count('prepare v2.11.2'), 2)

    def test_notice_failure_is_retried_without_blocking_the_candidate(self):
        self.release('v2.11.2')
        (self.fixture / 'gh-fails').touch()
        result = self.run_main(); self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('notice not delivered', result.stdout)
        self.assertTrue((self.state / 'candidate-ready.json').exists())
        (self.fixture / 'gh-fails').unlink()
        self.assertEqual(self.run_main().returncode, 0)
        self.assertIn('--approve v2.11.2', self.issues()['1']['body'])
        self.assertEqual(self.calls(), ['prepare v2.11.2'])


@unittest.skipUnless(os.name == 'posix', 'POSIX state permissions')
class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'; self.state.mkdir(mode=0o700)
        self.evidence = self.state / 'candidate.AbCd1234'; self.evidence.mkdir()
        images = {'api': {'image': API, 'pull_policy': 'never'}}
        report = {'status': 'checks_passed', 'tag': 'v2.11.9', 'source_commit': 'c' * 40, 'images': images,
                  'review_basis': {'profile': 'v2.11.629'}, 'guard_sha256': '0' * 64, 'runtime_config_sha256': '1' * 64}
        (self.evidence / 'checks.json').write_text(json.dumps(report))
        (self.evidence / 'images.json').write_text(json.dumps({'services': images}))
        self.checks = hashlib.sha256((self.evidence / 'checks.json').read_bytes()).hexdigest()

    def gate(self, *args):
        return subprocess.run([sys.executable, '-B', '-c', embedded('PYGATE'), *args],
                              capture_output=True, text=True, timeout=30)

    def receipt(self):
        result = self.gate('receipt', str(self.state), 'v2.11.9', 'c' * 40, str(self.evidence), self.checks,
                           '/w/update-' + 'c' * 40, BASE, 'passed')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_evidence_withdraws_approval(self):
        self.receipt()
        self.assertEqual(self.gate('approve', str(self.state), 'v2.11.9', 'owner').returncode, 0)
        self.assertEqual(self.gate('approved', str(self.state), 'v2.11.9', 'c' * 40).returncode, 0)
        (self.evidence / 'checks.json').write_text('{}')
        self.assertEqual(self.gate('approved', str(self.state), 'v2.11.9', 'c' * 40).returncode, 2)
        self.assertFalse((self.state / 'approval.json').exists())

    def test_edited_approval_or_receipt_is_rejected(self):
        self.receipt()
        self.assertEqual(self.gate('approve', str(self.state), 'v2.11.9', 'owner').returncode, 0)
        approval = json.loads((self.state / 'approval.json').read_text())
        approval['commit'] = 'd' * 40
        (self.state / 'approval.json').write_text(json.dumps(approval))
        self.assertEqual(self.gate('approved', str(self.state), 'v2.11.9', 'c' * 40).returncode, 2)
        self.assertEqual(self.gate('approve', str(self.state), 'v2.11.9', 'owner').returncode, 0)
        receipt = json.loads((self.state / 'candidate-ready.json').read_text())
        receipt['candidate_dir'] = '/elsewhere'
        (self.state / 'candidate-ready.json').write_text(json.dumps(receipt))
        self.assertEqual(self.gate('approved', str(self.state), 'v2.11.9', 'c' * 40).returncode, 2)

    def test_consume_is_single_use_and_evidence_outside_state_is_rejected(self):
        self.receipt()
        self.assertEqual(self.gate('approve', str(self.state), 'v2.11.9', 'owner').returncode, 0)
        self.assertEqual(self.gate('consume', str(self.state)).returncode, 0)
        self.assertEqual(self.gate('approved', str(self.state), 'v2.11.9', 'c' * 40).returncode, 3)
        self.assertNotEqual(self.gate('consume', str(self.state)).returncode, 0)
        outside = Path(self.temp.name) / 'candidate.Outside1'; shutil.copytree(self.evidence, outside)
        (self.state / 'candidate-ready.json').unlink()
        result = self.gate('receipt', str(self.state), 'v2.11.9', 'c' * 40, str(outside), self.checks, '/w', '-', 'passed')
        self.assertIn('evidence path rejected', result.stderr)


if __name__ == '__main__':
    unittest.main()
