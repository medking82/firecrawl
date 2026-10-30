import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from unittest.mock import Mock, patch

import notify

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def report(findings=False):
    rows = [{'target': '/app', 'type': 'npm', 'id': 'CVE-example', 'package': 'example',
             'installed_version': '1.0.0', 'severity': 'HIGH', 'fixed_version': '2.0.0'}] if findings else []
    return {'status': 'findings' if findings else 'no_high_critical_findings',
            'completed_at': NOW.isoformat(), 'findings_count': len(rows) * 3,
            'db': {'fixture': 'ignored'},
            'images': [{'container': name, 'image_id': 'sha256:' + 'a' * 64,
                        'status': 'findings' if findings else 'no_high_critical_findings',
                        'checked_at': NOW.isoformat(), 'findings': copy.deepcopy(rows)}
                       for name in sorted(notify.TARGETS)]}


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.report = self.root / 'latest.json'
        self.state = self.root / 'notifications'
        self.send = Mock()
        self.errors = io.StringIO()
        self.enterContext(redirect_stderr(self.errors))
        if os.name != 'posix':
            # Exercise persistence on Windows, without pretending chmod proves privacy.
            def directory(path):
                path.mkdir(parents=True, exist_ok=True)
                return path
            self.enterContext(patch.object(notify, 'private_dir', side_effect=directory))

    def write(self, value):
        self.report.write_text(json.dumps(value), encoding='utf-8')
        if os.name == 'posix':
            self.report.chmod(0o600)

    def run_consumer(self, **kwargs):
        return notify.consume(self.report, self.state, self.send, now=kwargs.get('now', NOW))

    def test_record_only_persists_event_without_desktop_delivery(self):
        self.write(report(findings=True))
        with patch.object(notify, 'desktop', side_effect=AssertionError('desktop delivery forbidden')):
            self.assertEqual(notify.consume(self.report,self.state,send=None,now=NOW),1)
            self.assertEqual(notify.consume(self.report,self.state,send=None,now=NOW),1)
        events=list((self.state/'events').glob('*.json'))
        self.assertEqual(len(events),1)
        self.assertEqual(json.loads(events[0].read_text())['delivery'],'recorded')

    def events(self):
        return [json.loads(path.read_text(encoding='utf-8')) for path in sorted((self.state / 'events').glob('*.json'))]

    def test_first_clean_notifies_once_and_does_not_modify_report(self):
        self.write(report())
        before = self.report.read_bytes()
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(self.run_consumer(), 0)
        self.send.assert_called_once()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['delivery'], 'accepted')
        self.assertEqual(self.report.read_bytes(), before)

    def test_scan_time_db_and_order_do_not_create_transitions(self):
        value = report(True)
        value['images'][0]['findings'] *= 2
        value['images'][0]['findings'][1]['id'] = 'CVE-other'
        value['findings_count'] += 1
        self.write(value)
        self.assertEqual(self.run_consumer(), 1)
        value['completed_at'] = (NOW + timedelta(minutes=1)).isoformat()
        value['db'] = {'new': 'metadata'}
        value['images'].reverse()
        for image in value['images']:
            image['checked_at'] = 'ignored'
            image['findings'].reverse()
        self.write(value)
        self.assertEqual(self.run_consumer(), 1)
        self.send.assert_called_once()

    def test_same_count_changed_finding_severity_fix_or_image_notifies(self):
        value = report(True)
        self.write(value)
        self.run_consumer()
        for field, replacement in [('id', 'CVE-new'), ('severity', 'CRITICAL'), ('fixed_version', '3.0.0')]:
            value['images'][0]['findings'][0][field] = replacement
            self.write(value)
            self.assertEqual(self.run_consumer(), 1)
        value['images'][0]['image_id'] = 'sha256:' + 'b' * 64
        self.write(value)
        self.run_consumer()
        self.assertEqual(self.send.call_count, 5)
        self.assertEqual(len(self.events()), 5)

    def test_findings_error_repeated_error_recovery_and_recurrence(self):
        for value, code in [(report(True), 1),
                            ({'status': 'error', 'completed_at': NOW.isoformat(), 'error': 'SECRET'}, 2),
                            ({'status': 'error', 'completed_at': NOW.isoformat(), 'error': 'OTHER SECRET'}, 2),
                            (report(), 0), (report(True), 1)]:
            self.write(value)
            self.assertEqual(self.run_consumer(), code)
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(len(self.events()), 4)
        self.assertNotIn('SECRET', json.dumps(self.events()) + self.errors.getvalue())

    def test_missing_report_deduplicates_but_remains_error_and_recovers(self):
        self.assertEqual(self.run_consumer(), 2)
        self.assertEqual(self.run_consumer(), 2)
        self.assertEqual(self.events()[0]['snapshot']['reason'], 'missing_report')
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('missing_report', self.errors.getvalue())
        self.write(report())
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(self.send.call_count, 2)

    def test_malformed_duplicate_keys_nonfinite_and_oversize_are_error_events(self):
        for raw in ['{', '{"status":"error","status":"findings"}', 'NaN', '[]', 'x' * (notify.MAX_BYTES + 1)]:
            self.write(report())
            self.report.write_text(raw, encoding='utf-8')
            self.assertEqual(self.run_consumer(), 2)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.events()[0]['snapshot']['reason'], 'invalid_or_unreadable_report')

    def test_stale_report_future_timestamp_and_recovery(self):
        self.write(report())
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(self.run_consumer(now=NOW + timedelta(hours=49)), 2)
        self.assertEqual(self.run_consumer(now=NOW + timedelta(hours=50)), 2)
        self.assertEqual(self.run_consumer(now=NOW - timedelta(minutes=6)), 2)
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(self.send.call_count, 4)

    def test_desktop_failure_retains_one_event_and_retries_before_dedup(self):
        self.write(report(True))
        self.send.side_effect = notify.NotifyError('SECRET')
        self.assertEqual(self.run_consumer(), 2)
        self.assertEqual(self.run_consumer(), 2)
        event = self.events()[0]
        self.assertEqual(event['delivery'], 'failed')
        self.assertFalse(json.loads((self.state / 'state.json').read_text())['delivered'])
        self.send.side_effect = None
        self.assertEqual(self.run_consumer(), 1)
        self.assertEqual(self.run_consumer(), 1)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['event_id'], event['event_id'])
        self.assertEqual(self.events()[0]['delivery'], 'accepted')
        self.assertNotIn('SECRET', self.errors.getvalue())

    def test_new_snapshot_supersedes_failed_delivery(self):
        self.write(report(True))
        self.send.side_effect = notify.NotifyError('failure')
        self.run_consumer()
        self.write(report())
        self.send.side_effect = None
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(sorted(event['delivery'] for event in self.events()), ['accepted', 'failed'])

    def test_failed_state_or_event_write_never_sends_and_can_retry(self):
        self.write(report())
        original = notify.atomic_json
        for target in ('state.json', 'events'):
            def fail(path, value):
                if path.name == target or path.parent.name == target:
                    raise OSError('disk full')
                return original(path, value)
            with patch.object(notify, 'atomic_json', side_effect=fail), self.assertRaises(OSError):
                self.run_consumer()
            self.send.assert_not_called()
        self.assertEqual(self.run_consumer(), 0)
        self.send.assert_called_once()

    def test_acknowledgement_failure_leaves_pending_for_retry(self):
        self.write(report())
        original = notify.atomic_json
        def fail(path, value):
            if path.name == 'state.json' and value['delivered']:
                raise OSError('disk full after desktop acceptance')
            return original(path, value)
        with patch.object(notify, 'atomic_json', side_effect=fail), self.assertRaises(OSError):
            self.run_consumer()
        self.assertFalse(json.loads((self.state / 'state.json').read_text())['delivered'])
        self.assertEqual(self.run_consumer(), 0)
        self.assertEqual(self.send.call_count, 2)  # At-least-once crash boundary.
        self.assertEqual(len(self.events()), 1)

    def test_corrupt_state_is_not_overwritten_or_silently_reset(self):
        self.write(report())
        self.run_consumer()
        path = self.state / 'state.json'
        original = json.loads(path.read_text())
        values = ['{', json.dumps({**original, 'event_id': '../escape'}),
                  json.dumps({**original, 'snapshot': {'changed': True}}),
                  json.dumps({**original, 'delivered': 'yes'})]
        for raw in values:
            path.write_text(raw, encoding='utf-8')
            self.send.reset_mock()
            with self.assertRaises((ValueError, notify.NotifyError)):
                self.run_consumer()
            self.assertEqual(path.read_text(), raw)
            self.send.assert_not_called()

    def test_main_reports_storage_failure_without_raw_exception(self):
        with patch.object(notify, 'private_dir', side_effect=OSError('SECRET')):
            self.assertEqual(notify.main(['--state-dir', str(self.state)]), 2)
        self.assertIn('consumer failed', self.errors.getvalue())
        self.assertNotIn('SECRET', self.errors.getvalue())

    def test_cli_exit_codes_and_delivery_failure_with_real_persistence(self):
        args = ['--report', str(self.report), '--state-dir', str(self.state)]
        # The real POSIX lock is tested separately; no desktop process is launched.
        with patch.object(notify, 'locked', side_effect=lambda _: nullcontext()), \
             patch.object(notify.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.write(report())
            with patch.object(notify, 'snapshot', wraps=lambda path, now: notify.normalize(notify.read_json(path), NOW)):
                self.assertEqual(notify.main(args), 0)
                self.assertEqual(notify.main(args), 0)
                self.assertEqual(run.call_count, 1)
                self.write(report(True))
                self.assertEqual(notify.main(args), 1)
                self.write({'status': 'error', 'completed_at': NOW.isoformat()})
                self.assertEqual(notify.main(args), 2)
                self.write(report())
                run.return_value.returncode = 1
                self.assertEqual(notify.main(args), 2)
                run.return_value.returncode = 0
                self.assertEqual(notify.main(args), 0)
        self.assertEqual(len(self.events()), 4)

    def test_updater_initial_success_silent_failure_recovery_and_recurrence(self):
        for status, calls, code in [('updated', 0, 0), ('already-current', 0, 0),
                                    ('error', 1, 2), ('error', 1, 2), ('updated', 2, 0),
                                    ('already-current', 2, 0), ('error', 3, 2)]:
            self.write({'status': status, 'error': 'SECRET', 'transaction': '/private/SECRET'})
            self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), code)
            self.assertEqual(self.send.call_count, calls)
        self.assertEqual(len(self.events()), 3)
        self.assertNotIn('SECRET', json.dumps(self.events()) + self.errors.getvalue())
        self.assertEqual(self.events()[0]['snapshot']['source'], 'updater')

    def test_updater_first_failure_retry_reuses_event(self):
        self.write({'status': 'error'})
        self.send.side_effect = notify.NotifyError('failed')
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        event_id = self.events()[0]['event_id']
        self.send.side_effect = None
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['event_id'], event_id)
        self.assertEqual(self.events()[0]['delivery'], 'accepted')

    def test_updater_missing_malformed_unknown_status_and_recovery(self):
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        for value in ([], {'status': 'prepared'}, {'status': None}):
            self.write(value)
            self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 2)
        self.write({'status': 'already-current'})
        self.assertEqual(notify.consume_updater(self.report, self.state, self.send, NOW), 0)
        self.assertEqual(self.send.call_count, 3)

    def test_cli_optional_updater_is_independent_and_errors_dominate(self):
        self.write(report())
        updater_report = self.root / 'updater-latest.json'
        updater_report.write_text('{"status":"error"}', encoding='utf-8')
        if os.name == 'posix':
            updater_report.chmod(0o600)
        args = ['--report', str(self.report), '--state-dir', str(self.state),
                '--updater-report', str(updater_report)]
        with patch.object(notify, 'locked', side_effect=lambda _: nullcontext()), \
             patch.object(notify.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
             patch.object(notify, 'snapshot', wraps=lambda path, now: notify.normalize(notify.read_json(path), NOW)):
            self.assertEqual(notify.main(args), 2)
            self.assertEqual(notify.main(args), 2)
            self.assertEqual(run.call_count, 2)  # One advisory, one updater event.
            # A broken advisory state must not silence the independent updater recovery.
            (self.state / 'state.json').write_text('{', encoding='utf-8')
            updater_report.write_text('{"status":"updated"}', encoding='utf-8')
            self.assertEqual(notify.main(args), 2)
            self.assertEqual(run.call_count, 3)
        updater_events = list((self.state / 'updater/events').glob('*.json'))
        self.assertEqual(len(updater_events), 2)

    def test_updater_success_does_not_read_or_enable_itself_without_option(self):
        args = ['--report', str(self.report), '--state-dir', str(self.state)]
        with patch.object(notify, 'locked', side_effect=lambda _: nullcontext()), \
             patch.object(notify, 'consume', return_value=0), patch.object(notify, 'consume_updater') as updater:
            self.assertEqual(notify.main(args), 0)
            updater.assert_not_called()


class ValidationTests(unittest.TestCase):
    def test_partial_inconsistent_and_invalid_reports_rejected(self):
        changes = [lambda x: x.update(status='no_findings'),
                   lambda x: x.update(findings_count=True),
                   lambda x: x.update(findings_count=0),
                   lambda x: x.update(images=x['images'][:-1]),
                   lambda x: x['images'][0].update(container=x['images'][1]['container']),
                   lambda x: x['images'][0].update(image_id='tag:latest'),
                   lambda x: x['images'][0].update(status='no_high_critical_findings'),
                   lambda x: x['images'][0]['findings'][0].update(severity='LOW'),
                   lambda x: x['images'][0]['findings'][0].update(fixed_version=None),
                   lambda x: x['images'][0]['findings'][0].pop('package'),
                   lambda x: x.update(completed_at='2026-10-30T12:00:00'),
                   lambda x: x.update(completed_at='broken')]
        for change in changes:
            value = report(True)
            change(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                notify.normalize(value, NOW)

    def test_desktop_uses_only_local_fixed_arguments_and_suppresses_output(self):
        with patch.object(notify.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            notify.desktop({'status': 'error', 'reason': '<b>SECRET</b>'})
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], 'notify-send')
        self.assertIn('--', args[0])
        self.assertNotIn('SECRET', str(args))
        self.assertEqual(kwargs['timeout'], 15)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertFalse(kwargs.get('shell', False))

    def test_missing_notify_send_nonzero_and_timeout_raise(self):
        for effect in (FileNotFoundError(), subprocess.TimeoutExpired('notify-send', 15), None):
            with self.subTest(effect=effect), patch.object(notify.subprocess, 'run',
                    side_effect=effect, return_value=Mock(returncode=1)), self.assertRaises(notify.NotifyError):
                notify.desktop({'status': 'error'})

    def test_updater_desktop_has_fixed_failure_and_recovery_copy(self):
        with patch.object(notify.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            notify.desktop({'source': 'updater', 'status': 'error', 'reason': 'SECRET'})
            notify.desktop({'source': 'updater', 'status': 'healthy'})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-2], 'Firecrawl Security Refresh Failed')
        self.assertEqual(run.call_args_list[1].args[0][-2], 'Firecrawl Security Refresh Recovered')
        self.assertNotIn('SECRET', str(run.call_args_list))

    def test_atomic_replace_failure_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            notify.atomic_json(path, {'old': True})
            with patch.object(notify.os, 'replace', side_effect=OSError('failure')), self.assertRaises(OSError):
                notify.atomic_json(path, {'new': True})
            self.assertEqual(json.loads(path.read_text()), {'old': True})
            self.assertEqual(list(Path(d).iterdir()), [path])

    @unittest.skipUnless(os.name == 'posix', 'real POSIX permissions, symlinks and flock')
    def test_private_storage_symlink_refusal_and_lock_contention(self):
        with tempfile.TemporaryDirectory() as d:
            root = notify.private_dir(Path(d) / 'private')
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            path = root / 'event.json'
            notify.atomic_json(path, {'private': True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.chmod(0o644)
            with self.assertRaises(notify.NotifyError):
                notify.read_json(path)
            path.chmod(0o600)
            link = root / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                notify.read_json(link)
            directory_link = Path(d) / 'dir-link'
            directory_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(notify.NotifyError):
                notify.private_dir(directory_link)
            with notify.locked(root):
                with self.assertRaises(notify.NotifyError):
                    with notify.locked(root):
                        self.fail('overlapping consumer acquired lock')
            root.chmod(0o755)
            with self.assertRaises(notify.NotifyError):
                notify.private_dir(root)

    @unittest.skipUnless(os.name == 'nt', 'Windows is deliberately unsupported for production storage')
    def test_windows_storage_fails_closed(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(notify.NotifyError):
            notify.private_dir(Path(d) / 'private')


if __name__ == '__main__':
    unittest.main()
