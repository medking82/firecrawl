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


    def test_transport_guard_follows_the_installed_upgrade_owner_pin(self):
        digest = 'a7' * 32
        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp) / 'firecrawl-upgrade'
            with patch.object(s, 'UPGRADE_OWNER', owner), patch.object(s, 'GUARDS', Path(tmp) / 'guards'):
                owner.write_text('#!/usr/bin/env bash\nguard_sha256=' + digest + '\n')
                self.assertEqual(s.reviewed_guard(), Path(tmp) / 'guards' / (digest + '.py'))
                for text in ('#!/usr/bin/env bash\n', '#!/usr/bin/env bash\nguard_sha256=' + digest + '\nguard_sha256=' + '8' * 64 + '\n',
                             '#!/usr/bin/env bash\n  guard_sha256=' + digest + '\n', '#!/usr/bin/env bash\nguard_sha256=' + digest.upper() + '\n'):
                    owner.write_text(text)
                    with self.assertRaises(RuntimeError): s.reviewed_guard()

    def test_docker29_null_dns_lists_are_not_configuration_drift(self):
        def config(**dns): return {'config': {}, 'host_config': {'NetworkMode': 'firecrawl_backend', **dns}, 'mounts': []}
        empty = config(Dns=[], DnsOptions=[], DnsSearch=[])
        self.assertEqual(s.normalize_api_configuration(config(Dns=None, DnsOptions=None, DnsSearch=None)), empty)
        self.assertNotEqual(s.normalize_api_configuration(config(Dns=['1.1.1.1'], DnsOptions=None, DnsSearch=None)), empty)
        self.assertEqual(s.normalize_api_configuration(config()), config())

    def test_orphan_build_container_removed_and_compose_containers_kept(self):
        rows = {
            'orphan': {'Config': {'Labels': {'com.docker.compose.project': 'firecrawl',
                                             'com.docker.compose.service': 'api'}}, 'State': {'Status': 'exited'}},
            'init': {'Config': {'Labels': {'com.docker.compose.project': 'firecrawl',
                                           'com.docker.compose.service': 'foundationdb-init',
                                           'com.docker.compose.container-number': '1'}}, 'State': {'Status': 'exited'}},
        }
        calls = []
        def fake(args, timeout=900):
            calls.append(args)
            if args[:2] == ['docker', 'ps']:
                return 'orphan\ninit\ngone' if 'label=com.docker.compose.project=firecrawl' in args else ''
            if args[:3] == ['docker', 'container', 'inspect']:
                if args[3] == 'gone': raise RuntimeError('command failed')
                return json.dumps([rows[args[3]]])
            if args[:2] == ['docker', 'rm']: return ''
            raise AssertionError(args)
        with patch.object(s, 'run', side_effect=fake):
            s.remove_orphan_containers()
        self.assertEqual([a for a in calls if a[:2] == ['docker', 'rm']], [['docker', 'rm', 'orphan']])

    def test_failed_build_still_removes_orphan_containers(self):
        config = json.dumps([{'Config': {'User': ''}, 'RootFS': {'Layers': []}}])
        def fake(args, timeout=900):
            return config if args[:3] == ['docker', 'image', 'inspect'] else ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(s, 'run', side_effect=fake), \
                patch.object(s.subprocess, 'run', return_value=s.subprocess.CompletedProcess([], 1)), \
                patch.object(s, 'remove_orphan_containers') as cleanup:
            with self.assertRaises(RuntimeError): s.build('image', Path(tmp), 'firecrawl')
            cleanup.assert_called_once()


if __name__ == '__main__': unittest.main()


class Readiness(unittest.TestCase):
    def serve(self, routes):
        import http.server, threading
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, body = routes.get(self.path, (404, b'{"success":false}'))
                self.send_response(status); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_new_core_path_and_legacy_v0_services(self):
        ok = (200, b'{"status":"ok"}')
        self.assertTrue(s.readiness(self.serve({'/health/readiness': ok})))
        self.assertTrue(s.readiness(self.serve({'/v0/health/readiness': ok})))
        self.assertFalse(s.readiness(self.serve({'/health/readiness': (200, b'{"status":"starting"}')})))

    def test_unreachable_port_is_not_ready(self):
        port = self.serve({})
        self.assertFalse(s.readiness(port))
