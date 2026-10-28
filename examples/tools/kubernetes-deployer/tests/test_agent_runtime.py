# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime selection and fail-closed Slack onboarding contracts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

CHART = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, CHART / 'files/scripts' / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class RuntimeTest(unittest.TestCase):
    def render(self, *args):
        return subprocess.run(['helm', 'template', 'sre', str(CHART),
            '--api-versions', 'agents.x-k8s.io/v1alpha1',
            '--set', 'platform.serviceAccountIssuerDiscovery.preexistingAnonymousAccess=true',
            *args], capture_output=True, text=True)

    def test_openclaw_requires_matching_image(self):
        r = self.render('--set', 'agent.runtime=openclaw', '--set', 'access.enabled=false')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('runtime image', r.stderr)

    def test_openclaw_has_native_workspace_and_node_inference_policy(self):
        r = self.render('--set', 'agent.runtime=openclaw', '--set', 'access.enabled=false',
            '--set', 'agent.image.multiarch=ghcr.io/nvidia/nemoclaw/openclaw-sandbox@sha256:' + 'a' * 64)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"mount_path": "/sandbox/.openclaw/workspace"', r.stdout)
        self.assertIn('/usr/local/bin/node', r.stdout)

    def test_slack_rejects_empty_allowlist(self):
        r = self.render('--set', 'agent.slack.enabled=true')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Slack requires', r.stderr)

    def test_openclaw_terminal_does_not_pass_hermes_skills(self):
        m = module('operator_client')
        with mock.patch.dict(os.environ, {'AGENT_RUNTIME': 'openclaw',
                'OPENSHELL_SANDBOX_NAME': 'sre', 'HERMES_SKILLS': 'kubernetes-sre'}):
            command = m.hermes_command([])
        self.assertEqual(command[command.index('--') + 1:], ['openclaw', 'tui'])
        self.assertEqual(command[command.index('--workdir') + 1], '/sandbox/.openclaw/workspace')

    def test_openclaw_model_must_use_selected_model_through_managed_inference(self):
        m = module('prepare_agent')
        good = {'agents': {'defaults': {'model': {'primary': 'inference/test-model'}}},
                'models': {'providers': {'inference': {'baseUrl': 'https://inference.local/v1'}}}}
        self.assertIsNone(m.verify_openclaw_model(good, 'test-model'))
        with self.assertRaises(ValueError):
            m.verify_openclaw_model(good, 'different-model')
        good['models']['providers']['inference']['baseUrl'] = 'https://external.invalid'
        with self.assertRaises(ValueError):
            m.verify_openclaw_model(good, 'test-model')

    def test_slack_config_limits_users_channels_and_disables_mutating_slack_tools(self):
        m = module('prepare_agent')
        cfg = m.slack_config('openclaw', {}, ['U123'], ['C123'])
        account = cfg['channels']['slack']['accounts']['default']
        self.assertEqual(account['allowFrom'], ['U123'])
        self.assertEqual(account['dmPolicy'], 'allowlist')
        self.assertEqual(account['groupPolicy'], 'allowlist')
        self.assertEqual(account['channels']['C123']['users'], ['U123'])
        self.assertTrue(account['channels']['C123']['requireMention'])
        self.assertTrue(account['channels']['C123']['enabled'])
        self.assertNotIn('allow', account['channels']['C123'])
        self.assertFalse(cfg['channels']['slack']['configWrites'])
        self.assertEqual(cfg['session']['dmScope'], 'per-channel-peer')
        self.assertNotIn('botToken', json.dumps(cfg))

    def test_slack_config_rejects_missing_or_wrong_id_allowlists(self):
        m = module('prepare_agent')
        for users, channels in [([], ['C123']), (['U123'], []), (['C123'], ['C123']), (['U123'], ['T123'])]:
            with self.subTest(users=users, channels=channels), self.assertRaises(ValueError):
                m.slack_config('openclaw', {}, users, channels)

    def test_hermes_slack_config_is_allowlisted_and_preserves_other_config(self):
        m = module('prepare_agent')
        cfg = m.slack_config('hermes', {'model': 'test'}, ['U123'], ['C123'])
        self.assertEqual(cfg['model'], 'test')
        self.assertTrue(cfg['platforms']['slack']['enabled'])
        self.assertTrue(cfg['platforms']['slack']['extra']['rich_blocks'])

    def test_preflight_rejects_cross_app_tokens_and_wrong_workspace(self):
        m = module('slack_preflight')
        auth = {'ok': True, 'team_id': 'T123', 'user_id': 'U456'}
        bot = {'ok': True, 'bot': {'app_id': 'A123'}}
        for workspace, app in [('C123', 'A123'), ('T123', 'A456')]:
            with self.subTest(workspace=workspace, app=app), self.assertRaises(ValueError):
                m.validate_identity(auth, bot, workspace, app)

    def test_disabling_slack_removes_stale_native_configuration(self):
        m = module('prepare_agent')
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            state = home / '.openclaw'
            state.mkdir()
            config = state / 'openclaw.json'
            config.write_text(json.dumps({'channels': {'slack': {'enabled': True}},
                                         'plugins': {'entries': {'slack': {'enabled': True}}}}))
            m.prepare('openclaw', {'enabled': False}, home)
            disabled = json.loads(config.read_text())
            self.assertFalse(disabled['channels']['slack']['enabled'])
            self.assertFalse(disabled['plugins']['entries']['slack']['enabled'])

    def test_preflight_success_never_returns_socket_url(self):
        m = module('slack_preflight')
        responses = [
            ({'ok': True, 'team_id': 'T123', 'user_id': 'U456', 'bot_id': 'B456'},
             'chat:write,app_mentions:read,im:history,channels:history'),
            ({'ok': True, 'bot': {'app_id': 'A123'}}, ''),
            ({'ok': True, 'channel': {'is_member': True, 'is_private': False}}, ''),
            ({'ok': True, 'url': 'wss://example.invalid/credential-bearing-url'}, ''),
        ]
        with mock.patch.object(m, 'api', side_effect=responses):
            result = m.check('xoxb-test', 'xapp-1-A123-test', 'C123', 'T123', 'A123')
        self.assertEqual(result['workspaceId'], 'T123')
        self.assertTrue(result['socketMode'])
        self.assertNotIn('credential-bearing-url', json.dumps(result))

    def test_slack_providers_use_secret_refs_and_scoped_network_policy(self):
        r = self.render('--set', 'agent.slack.enabled=true', '--set', 'agent.slack.allowedUsers[0]=U123',
            '--set', 'agent.slack.allowedChannels[0]=C123', '--set', 'agent.slack.secretRef.name=slack-test')
        self.assertEqual(r.returncode, 0, r.stderr)
        # Inspect rendered manifests, not embedded Python source.
        import yaml
        docs = list(yaml.safe_load_all(r.stdout))
        job = next(d for d in docs if d and d.get('kind') == 'Job' and d['metadata']['name'].endswith('-bootstrap'))
        env = {e['name']: e for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['SLACK_APP_TOKEN']['valueFrom']['secretKeyRef'], {'name': 'slack-test', 'key': 'app-token'})
        runtime_volume = next(v for v in job['spec']['template']['spec']['volumes'] if v['name'] == 'runtime')
        mounted = {v['path'] for v in runtime_volume['configMap']['items']}
        self.assertTrue({'slack-provider.yaml', 'prepare_agent.py'} <= mounted)
        cm = next(d for d in docs if d and 'policy.yaml' in d.get('data', {}))
        policy = yaml.safe_load(cm['data']['policy.yaml'])
        hosts = {e['host'] for e in policy['network_policies']['slack']['endpoints']}
        self.assertEqual(hosts, {'slack.com', 'api.slack.com', 'wss-primary.slack.com', 'wss-backup.slack.com'})


if __name__ == '__main__':
    unittest.main()
