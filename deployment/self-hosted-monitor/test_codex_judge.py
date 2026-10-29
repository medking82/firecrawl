import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import codex_judge as judge


class CodexJudgeContracts(unittest.TestCase):
    def test_selection_preserves_native_model_effort_and_fast_tier_only(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_reasoning_effort="max"\nservice_tier="priority"\n[hooks]\nSessionStart=[]\n')
            self.assertEqual(judge.selection(home), {
                'model': 'gpt-6.1-sol', 'model_reasoning_effort': 'max', 'service_tier': 'priority'})

    def test_non_native_model_and_provider_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'config.toml').write_text('model="claude-cli/claude-sonnet-5-5"')
            with self.assertRaisesRegex(ValueError, 'native_codex_model_required'):
                judge.selection(home)
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="other"')
            with self.assertRaisesRegex(ValueError, 'native_openai_provider_required'):
                judge.selection(home)

    def test_catalog_keeps_model_identity_and_removes_tools_without_mutating_cache(self):
        original = {'slug': 'gpt-6.1-sol', 'apply_patch_tool_type': 'freeform',
                    'shell_type': 'unified_exec', 'experimental_supported_tools': ['clock'],
                    'tool_mode': 'code_mode_only', 'context_window': 262144}
        result = judge.text_only_catalog({'models': [original]}, 'gpt-6.1-sol')['models'][0]
        self.assertEqual(result['slug'], original['slug'])
        self.assertEqual(result['context_window'], original['context_window'])
        self.assertIsNone(result['apply_patch_tool_type'])
        self.assertEqual(result['experimental_supported_tools'], [])
        self.assertEqual(result['shell_type'], 'disabled')
        self.assertEqual(result['tool_mode'], 'direct')
        self.assertEqual(original['apply_patch_tool_type'], 'freeform')
        with self.assertRaisesRegex(ValueError, 'not_in_native_catalog'):
            judge.text_only_catalog({'models': []}, 'missing')

    def test_command_uses_actual_cli_and_disables_config_hooks_tools_api_auth(self):
        args = judge.command(Path('/private/probe'), {'model': 'gpt-6.1-sol', 'model_reasoning_effort': 'max'})
        self.assertEqual(args[:2], ['/home/marck/.local/bin/codex', 'exec'])
        self.assertIn('--ignore-user-config', args)
        self.assertIn('--ignore-rules', args)
        self.assertIn('--ephemeral', args)
        self.assertIn('read-only', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        for setting in ['features.hooks=false', 'features.plugins=false', 'features.apps=false',
                        'features.shell_tool=false', 'mcp_servers={}', 'web_search="disabled"',
                        'forced_login_method="chatgpt"', 'project_doc_max_bytes=0',
                        'tools.experimental_request_user_input.enabled=false']:
            self.assertIn(setting, args)
        self.assertEqual(args[-1], '-')

    def test_rejects_any_tool_item_even_when_turn_completes(self):
        for kind in ['command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'future_tool']:
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'unexpected_codex_tool_event'):
                judge.validate_events(json.dumps({'type': 'item.completed', 'item': {'type': kind}})
                                      + '\n' + json.dumps({'type': 'turn.completed', 'usage': {}}))

    def test_requires_successful_completed_turn(self):
        for event in ['error', 'turn.failed']:
            with self.assertRaisesRegex(RuntimeError, 'codex_turn_failed'):
                judge.validate_events(json.dumps({'type': event}))
        with self.assertRaisesRegex(RuntimeError, 'not_completed'):
            judge.validate_events(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message'}}))
        self.assertEqual(judge.validate_events('\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started'}, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{}'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100}},
        ])), {'input_tokens': 100})

    def test_api_auth_is_rejected_before_starting_process(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'auth.json').write_text('{"auth_mode":"apikey"}')
            with patch.object(judge.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'existing_subscription_auth_required'):
                    judge.judge('system', 'prompt', codex_home=temp)
                spawn.assert_not_called()

    def test_real_subprocess_boundary_strips_api_credentials_and_uses_stdin_once(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'auth.json').write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"private-test-token"}}')
            (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_reasoning_effort="max"')
            (home / 'models_cache.json').write_text('{"models":[{"slug":"gpt-6.1-sol"}]}')
            def spawn(args, **kwargs):
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                self.assertNotIn('CODEX_API_KEY', kwargs['env'])
                self.assertNotIn('OPENAI_BASE_URL', kwargs['env'])
                self.assertEqual(kwargs['env']['CODEX_HOME'], str(home))
                self.assertTrue(kwargs['start_new_session'])
                self.assertNotIn('private-test-token', ' '.join(args))
                self.assertNotIn('private-page-text', ' '.join(args))
                answer = Path(args[args.index('--output-last-message') + 1])
                answer.write_text('{"meaningful":true,"confidence":"high","reason":"Price changed","meaningfulChanges":[]}')
                kwargs['stdout'].write(b'{"type":"turn.completed","usage":{"input_tokens":100}}\n')
                process = Mock(returncode=0)
                def communicate(value, timeout):
                    self.assertIn(b'private-page-text', value)
                    self.assertEqual(timeout, 12)
                process.communicate.side_effect = communicate
                return process
            with patch.dict(judge.os.environ, {'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret',
                                               'OPENAI_BASE_URL': 'https://forbidden.invalid'}):
                with patch.object(judge.subprocess, 'Popen', side_effect=spawn) as process:
                    result = judge.judge('private-page-text', 'goal', codex_home=home, timeout=12)
                    self.assertEqual(result['backend'], 'codex-cli')
                    self.assertEqual(result['tool_calls'], 0)
                    process.assert_called_once()


if __name__ == '__main__':
    unittest.main()
