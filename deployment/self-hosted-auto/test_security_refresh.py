import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import security_refresh as s


class Policy(unittest.TestCase):
    def test_readiness_retries_startup_connection_reset(self):
        class P:
            PORTS={'fixture':123}
        with patch.object(P,'readiness',side_effect=[ConnectionResetError(),True],create=True) as probe, patch.object(s.time,'sleep'):
            s.ready(P,'fixture')
            self.assertEqual(probe.call_count,2)

    def finding(self, **extra):
        return dict(target='debian 12', type='debian', id='CVE-existing', package='p', severity='HIGH', **extra)

    def test_vendor_version_change_does_not_hide_or_invent_advisory(self):
        old = self.finding(installed_version='1')
        new = dict(old, installed_version='2', target='debian 12.16')
        s.candidate_policy([old], [new])

    def test_new_advisory_and_severity_escalation_blocked(self):
        old = self.finding()
        for new in (dict(old, id='new'), dict(old, severity='CRITICAL')):
            with self.assertRaises(RuntimeError): s.candidate_policy([old], [new])

    def test_brotli_always_blocked_even_in_baseline(self):
        f = dict(self.finding(), id='CVE-2026-111497')
        with self.assertRaises(RuntimeError): s.candidate_policy([f], [f])

    def test_incomplete_findings_fail_closed(self):
        with self.assertRaises(KeyError): s.candidate_policy([], [{'id':'x'}])

    def test_recovery_is_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class P: BROWSER = root
            journal = root/'transaction.json'
            record = dict(status='applying', files={}, old={p:'old' for p in s.PROJECTS}, new={p:'new' for p in s.PROJECTS})
            s.save(journal, record); s.save(root/'admission-paused', {'owner':str(journal)})
            record['snapshots'] = {p:{} for p in s.PROJECTS}; s.save(journal,record)
            with patch.object(s,'verify_runtime'), patch.object(s,'recovery_image',return_value=None), patch.object(s,'apply_image') as apply:
                s.recover(P, journal)
                self.assertEqual(apply.call_count, 3)
                self.assertEqual(json.loads(journal.read_text())['status'], 'rolled-back')
                self.assertFalse((root/'admission-paused').exists())
                s.recover(P, journal)
                self.assertEqual(apply.call_count, 3)

    def test_unknown_image_preserves_gate_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            class P: BROWSER=root
            journal=root/'transaction.json'
            s.save(journal, dict(status='applying',files={},snapshots={p:{} for p in s.PROJECTS},old={p:'old' for p in s.PROJECTS},new={p:'new' for p in s.PROJECTS}))
            s.save(root/'admission-paused', {'owner':str(journal)})
            with patch.object(s,'verify_runtime'), patch.object(s,'recovery_image',return_value='foreign'), patch.object(s,'apply_image') as apply:
                with self.assertRaises(RuntimeError): s.recover(P,journal)
                apply.assert_not_called()
                self.assertTrue((root/'admission-paused').exists())

    def test_pre_stop_abort_releases_gate_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            class P: BROWSER=root
            journal=root/'transaction.json';s.save(journal,{'status':'gated'})
            s.save(root/'admission-paused',{'owner':str(journal)})
            with patch.object(s,'apply_image') as apply:
                s.recover(P,journal);apply.assert_not_called()
                self.assertFalse((root/'admission-paused').exists())

    def test_foreign_gate_and_runtime_drift_block_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            class P: BROWSER=root
            journal=root/'transaction.json'
            s.save(journal,dict(status='applying',files={},snapshots={p:{} for p in s.PROJECTS}))
            s.save(root/'admission-paused',{'owner':'foreign'})
            with patch.object(s,'apply_image') as apply:
                with self.assertRaises(RuntimeError):s.recover(P,journal)
                apply.assert_not_called()
                s.save(root/'admission-paused',{'owner':str(journal)})
                with patch.object(s,'verify_runtime',side_effect=RuntimeError('drift')):
                    with self.assertRaises(RuntimeError):s.recover(P,journal)
                apply.assert_not_called()

    def test_terminal_recovery_checks_runtime_before_gate_release(self):
        for status in ('committed','rolled-back'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                class P: BROWSER=root
                journal=root/'transaction.json'
                s.save(journal,dict(status=status,files={},snapshots={p:{} for p in s.PROJECTS},
                                   old={p:'old' for p in s.PROJECTS},new={p:'new' for p in s.PROJECTS}))
                s.save(root/'admission-paused',{'owner':str(journal)})
                with patch.object(s,'verify_runtime',side_effect=RuntimeError('drift')):
                    with self.assertRaises(RuntimeError): s.recover(P,journal)
                self.assertTrue((root/'admission-paused').exists())


if __name__ == '__main__': unittest.main()
