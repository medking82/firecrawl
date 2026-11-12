"""Check real JavaScript parsing and fail-closed syntax-equivalent admission."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name('firecrawl_transport_guard.py')
spec = importlib.util.spec_from_file_location('syntax_guard', PATH)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class SyntaxAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.profile = copy.deepcopy(guard.TRANSPORT_PROFILES['v2.11.704'])
        self.profile['syntax_hashes'] = dict.fromkeys(self.profile['hashes'], 'a' * 64)
        # Additional helpers are part of the same complete audited program.
        self.profile['syntax_hashes']['/app/dist/src/lib/reserved-capacity.js'] = 'b' * 64
        self.probe = {
            'hosted_bash_present': False,
            'hashes': copy.deepcopy(self.profile['hashes']),
            'dependencies': copy.deepcopy(self.profile['dependencies']),
            'predicates': dict(guard.EXPECTED_PREDICATES),
            'safeguards': dict.fromkeys(('private_bypass_absent_or_empty', 'db_auth_canonical_false',
                'dotenv_absent', 'alternate_dotenv_unconfigured', 'expected_working_directory',
                'node_options_unconfigured'), True),
        }
        self.probe['hashes'][next(iter(self.probe['hashes']))] = '0' * 64
        self.syntax = {'syntax_hashes': copy.deepcopy(self.profile['syntax_hashes'])}
        self.container = {'id': 'c' * 64, 'image': 'sha256:' + 'd' * 64, 'running': True}
        self.calls = []

    def verify(self):
        def response(args, stage, deadline):
            self.calls.append(args)
            if args[1] == 'inspect':
                return copy.deepcopy(self.container)
            if 'syntax_hashes' in args[-1]:
                return copy.deepcopy(self.syntax)
            if 'undici_lib_sha256' in args[-1]:
                return {'undici_lib_sha256': self.profile['undici_lib_sha256'], 'file_count': 116}
            return copy.deepcopy(self.probe)
        with patch.object(guard, 'TRANSPORT_PROFILES', {'audited': self.profile}), \
                patch.object(guard, 'run_json', side_effect=response):
            return guard.verify()

    def test_equivalent_syntax_admits_changed_raw_hashes(self):
        result = self.verify()
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['fingerprint_match'], 'syntax')
        self.assertEqual(result['syntax_hashes_checked'], len(self.profile['syntax_hashes']))

    def test_changed_executable_code_rejected(self):
        self.syntax['syntax_hashes'][next(iter(self.profile['hashes']))] = 'f' * 64
        self.assertFalse(self.verify()['verified'])

    def test_extra_helper_required_even_when_base_hashes_match(self):
        self.probe['hashes'] = copy.deepcopy(self.profile['hashes'])
        self.syntax['syntax_hashes']['/app/dist/src/lib/reserved-capacity.js'] = 'f' * 64
        self.assertFalse(self.verify()['verified'])

    def test_extra_or_missing_syntax_keys_rejected(self):
        self.syntax['syntax_hashes']['/app/unknown.js'] = 'a' * 64
        self.assertFalse(self.verify()['verified'])
        del self.syntax['syntax_hashes']['/app/unknown.js']
        del self.syntax['syntax_hashes']['/app/dist/src/lib/reserved-capacity.js']
        self.assertFalse(self.verify()['verified'])

    def test_dependencies_and_config_still_required(self):
        self.probe['dependencies']['node_major'] = 23
        self.assertFalse(self.verify()['verified'])
        self.assertFalse(any('syntax_hashes' in call[-1] for call in self.calls))
        self.probe['dependencies'] = copy.deepcopy(self.profile['dependencies'])
        self.probe['safeguards']['private_bypass_absent_or_empty'] = False
        self.assertFalse(self.verify()['verified'])


@unittest.skipUnless(shutil.which('node'), 'Node required for real parser tests')
class JavaScriptSyntaxTests(unittest.TestCase):
    def hashes(self, *sources):
        program = guard.syntax_normalizer_source() + '''
const sources = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(sources.map(syntaxHash)));
'''
        result = subprocess.run(['node', '-e', program], input=json.dumps(sources),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_comments_and_formatting_are_equivalent(self):
        a, b = self.hashes('function f(x){return x + 1;}\n',
                           '// comment\nfunction f (x) { /* note */ return x+1; }\n//# sourceMappingURL=other.js.map')
        self.assertEqual(a, b)

    def test_asi_strings_regex_and_tagged_templates_remain_significant(self):
        pairs = [('function f(){return\n1;}', 'function f(){return 1;}'),
                 ('fetch("https://public.example")', 'fetch("http://127.0.0.1")'),
                 ('const r=/foo/i;', 'const r=/foo/g;'),
                 ('tag`\\n`', 'tag`\n`'),
                 ('"use strict"; x=1;', '("use strict"); x=1;')]
        for a, b in pairs:
            with self.subTest(a=a):
                first, second = self.hashes(a, b)
                self.assertNotEqual(first, second)

    def test_nonfinite_numbers_do_not_collide_with_null(self):
        a, b = self.hashes('const x=null;', 'const x=1e400;')
        self.assertNotEqual(a, b)

    def test_legacy_profile_cannot_admit_a_hosted_shell(self):
        profile = copy.deepcopy(guard.TRANSPORT_PROFILES['v2.11.704'])
        probe = {'hashes': profile['hashes'], 'dependencies': profile['dependencies'],
                 'predicates': dict(guard.EXPECTED_PREDICATES), 'safeguards': {},
                 'hosted_bash_present': True}
        container = {'id': 'c' * 64, 'image': 'sha256:' + 'd' * 64, 'running': True}
        def response(args, stage, deadline):
            if args[1] == 'inspect': return container
            return probe
        with patch.object(guard, 'TRANSPORT_PROFILES', {'legacy': profile}), \
                patch.object(guard, 'run_json', side_effect=response):
            result = guard.verify()
        self.assertFalse(result['verified'])
        self.assertEqual(result['error'], 'shell_profile_required')

    def test_external_transitive_dependency_and_worker_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / 'worker/shell-worker.js'
            modules = worker.parent / 'node_modules'
            for name, deps in [('just-bash', {'formatter': '*'}),
                               ('formatter', {'leaf': '*'}), ('leaf', {})]:
                package = modules / name
                package.mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({
                    'name': name, 'version': '1.0.0', 'main': 'index.cjs', 'dependencies': deps}))
                (package / 'index.cjs').write_text('module.exports=1;')
            def digest():
                program = guard.just_bash_tree_probe_source().replace(
                    "'/app/dist/src/lib/hosted-bash/shell-worker.js'", json.dumps(str(worker)))
                result = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)['just_bash_tree_sha256']
            before = digest()
            (modules / 'leaf/index.cjs').write_text('module.exports=2;')
            self.assertNotEqual(before, digest())
            # A worker-local copy takes precedence over an unrelated outer copy.
            outer = root / 'node_modules/just-bash'
            outer.mkdir(parents=True)
            (outer / 'package.json').write_text('{"name":"just-bash","main":"index.cjs"}')
            (outer / 'index.cjs').write_text('module.exports="unrelated";')
            after = digest()
            (outer / 'index.cjs').write_text('module.exports="changed";')
            self.assertEqual(after, digest())

    def test_bigint_is_supported(self):
        a, b = self.hashes('const a=123n;', 'const a = 123n; // note')
        self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main()
