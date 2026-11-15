"""Artifact validation keeps source builds separate from image admission."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('firecrawl_update.sh').read_text()
CODE = SOURCE.split("<<'PYARTIFACT'\n", 1)[1].split('\nPYARTIFACT', 1)[0]

class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / 'context'
        self.context.mkdir()
        (self.context / 'patch.cjs').write_text('reviewed')
        self.path = self.root / 'artifact.json'
        self.image = 'sha256:' + 'a' * 64
        self.commit = 'b' * 40
        self.guard = 'c' * 64
        self.value = dict(schema=1, tag='v2.11.743', commit=self.commit,
                          guard_sha256=self.guard, qualification_exit=0,
                          qualification_host='PC WSL Ubuntu-26.04',
                          base_image=self.image, patched_image=self.image,
                          patch_inputs={'patch.cjs': hashlib.sha256(b'reviewed').hexdigest()})

    def validate(self, digest=None, source=None):
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)
        argv = ['-', str(self.path), digest or hashlib.sha256(self.path.read_bytes()).hexdigest(),
                'v2.11.743', self.commit, self.guard, str(self.context)]
        metadata = [{'Id':self.image,'Config':{'Env':['FIRECRAWL_BUILD_SHA='+(source or self.commit)]}}]
        with patch('sys.argv',argv), patch('subprocess.check_output',return_value=json.dumps(metadata).encode()):
            exec(compile(CODE,'artifact-validator','exec'), {})

    def test_qualified_artifact(self):
        self.validate()

    def test_descriptor_digest_changed(self):
        with self.assertRaises(SystemExit): self.validate('d'*64)

    def test_source_binding_changed(self):
        with self.assertRaises(SystemExit): self.validate(source='e'*40)

    def test_mutable_tag_denied(self):
        self.value['patched_image']='firecrawl:latest'
        with self.assertRaises(SystemExit): self.validate()

    def test_patch_inputs_changed(self):
        (self.context/'patch.cjs').write_text('unknown')
        with self.assertRaises(SystemExit): self.validate()

    def test_guard_binding_changed(self):
        self.value['guard_sha256']='f'*64
        with self.assertRaises(SystemExit): self.validate()

    def test_failed_qualification_denied(self):
        self.value['qualification_exit']=1
        with self.assertRaises(SystemExit): self.validate()

    def test_security_refresh_default_and_explicit_deferral(self):
        import subprocess
        section=SOURCE.split('  if [[ "${FIRECRAWL_SKIP_SECURITY_REFRESH:-0}" == 1 ]]; then',1)[1].split('\n  gate notify',1)[0]
        body='if [[ "${FIRECRAWL_SKIP_SECURITY_REFRESH:-0}" == 1 ]]; then'+section
        for mode, expected in [('0','called'),('1','deferred explicitly')]:
            script='set -e; security_refresh=x; security_journals=x; log() { printf "%s\\n" "$*"; }; timeout() { printf "called\\n"; }; FIRECRAWL_SKIP_SECURITY_REFRESH='+mode+'; '+body
            p=subprocess.run(['bash','-c',script],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertIn(expected,p.stdout)
            if mode=='1': self.assertNotIn('called',p.stdout)
