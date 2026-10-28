# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prebuilt image plan delivery and reversible native response instructions."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import yaml

CHART = Path(__file__).resolve().parents[1]


def preparation():
    spec = importlib.util.spec_from_file_location('prepare_slack_response', CHART / 'files/scripts/prepare_agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SlackResponseTest(unittest.TestCase):
    def test_native_slack_thread_followups_and_progress(self):
        config = preparation().slack_config('openclaw', {}, ['U123'], ['C123'])['channels']['slack']
        self.assertEqual(config['implicitMentions'], {'replyToBot':True, 'threadParticipation':True})
        self.assertEqual(config['thread']['historyScope'], 'thread')
        self.assertEqual(config['streaming']['mode'], 'progress')
        self.assertFalse(config['allowBots'])
        self.assertTrue(config['accounts']['default']['channels']['C123']['requireMention'])

    def test_startup_diagnostics_exclude_exception_values(self):
        m = preparation()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = home / '.openclaw/workspace'
            workspace.mkdir(parents=True)
            try:
                raise PermissionError(13, 'sensitive credential', '/secret/path')
            except PermissionError as error:
                m.record_preparation_failure('openclaw', error, home)
            path = workspace / '.sre-startup-failure.json'
            text = path.read_text()
            evidence = json.loads(text)
            self.assertEqual(evidence['error_type'], 'PermissionError')
            self.assertEqual(evidence['errno'], 13)
            self.assertTrue(evidence['frames'])
            self.assertNotIn('sensitive', text)
            self.assertNotIn('/secret', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_slack_sdk_aliases_preserve_resolver_revision_without_real_tokens(self):
        m = preparation()
        for revision in ['', 'v12_', 's' + 'a' * 64 + '_']:
            config = m.slack_config('openclaw', {}, ['U123'], ['C123'])
            env = {key: 'openshell:resolve:env:' + revision + key
                   for key in ['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN']}
            m.configure_slack_credentials(config, env)
            account = config['channels']['slack']['accounts']['default']
            self.assertEqual(account['botToken'], 'xoxb-OPENSHELL-RESOLVE-ENV-' + revision + 'SLACK_BOT_TOKEN')
            self.assertEqual(account['appToken'], 'xapp-OPENSHELL-RESOLVE-ENV-' + revision + 'SLACK_APP_TOKEN')
        for value in ['', 'xoxb-actual-credential', 'openshell:resolve:env:WRONG_KEY']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.configure_slack_credentials(config, {'SLACK_BOT_TOKEN': value})

    def test_native_persona_preserved_and_style_can_be_disabled(self):
        m = preparation()
        for runtime, relative in [('hermes', '.hermes/SOUL.md'), ('openclaw', '.openclaw/workspace/SOUL.md')]:
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                path = home / relative
                path.parent.mkdir(parents=True)
                path.write_text('Existing team persona.\n')
                slack = {'enabled': True, 'responseStyle': 'incident-summary'}
                m.configure_response_style(runtime, slack, home)
                once = path.read_text()
                self.assertIn('Existing team persona.', once)
                self.assertIn('Human action', once)
                self.assertIn('Do not claim a Rootly', once)
                m.configure_response_style(runtime, slack, home)
                self.assertEqual(path.read_text(), once)
                m.configure_response_style(runtime, {'enabled': True, 'responseStyle': 'standard'}, home)
                self.assertEqual(path.read_text().strip(), 'Existing team persona.')
                m.configure_response_style(runtime, slack, home)
                m.configure_response_style(runtime, {'enabled': False, 'responseStyle': 'incident-summary'}, home)
                self.assertNotIn(m.INCIDENT_START, path.read_text())

    def test_standard_does_not_create_or_rewrite_persona(self):
        m = preparation()
        with tempfile.TemporaryDirectory() as tmp:
            m.configure_response_style('hermes', {}, Path(tmp))
            self.assertFalse((Path(tmp) / '.hermes/SOUL.md').exists())

    def test_symlink_and_unknown_style_rejected(self):
        m = preparation()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / '.hermes').mkdir()
            target = home / 'outside'
            target.write_text('preserve')
            (home / '.hermes/SOUL.md').symlink_to(target)
            with self.assertRaises(ValueError):
                m.configure_response_style('hermes', {'enabled': True, 'responseStyle': 'incident-summary'}, home)
            self.assertEqual(target.read_text(), 'preserve')
            with self.assertRaises(ValueError):
                m.configure_response_style('hermes', {'responseStyle': 'unknown'}, home)

    def test_environment_plan_works_without_image_artifact_and_rejects_bad_encoding(self):
        m = preparation()
        expected = json.loads((CHART / 'files/messaging/openclaw-slack.json').read_text())
        encoded = base64.b64encode(json.dumps(expected).encode()).decode()
        with mock.patch.dict(os.environ, {'NEMOCLAW_MESSAGING_PLAN_B64': encoded}):
            self.assertEqual(m.read_messaging_plan(), expected)
        with mock.patch.dict(os.environ, {'NEMOCLAW_MESSAGING_PLAN_B64': 'invalid!'}):
            with self.assertRaises(ValueError):
                m.read_messaging_plan()

    def test_helm_delivers_native_plan_without_custom_image_acknowledgement(self):
        for runtime in ['hermes', 'openclaw']:
            with self.subTest(runtime=runtime):
                args = ['helm', 'template', 'sre', str(CHART), '--api-versions', 'agents.x-k8s.io/v1alpha1',
                        '--set', 'platform.serviceAccountIssuerDiscovery.preexistingAnonymousAccess=true',
                        '--set', 'access.enabled=false', '--set', 'agent.runtime=' + runtime,
                        '--set', 'agent.image.multiarch=ghcr.io/nvidia/nemoclaw/' + runtime + '-sandbox@sha256:' + 'a'*64,
                        '--set', 'agent.slack.enabled=true', '--set', 'agent.slack.allowedUsers[0]=U123',
                        '--set', 'agent.slack.allowedChannels[0]=C123', '--set', 'agent.slack.secretRef.name=slack',
                        '--set', 'agent.slack.responseStyle=incident-summary']
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                docs = list(yaml.safe_load_all(result.stdout))
                config = next(json.loads(d['data']['bootstrap-config.json']) for d in docs
                              if d and 'bootstrap-config.json' in d.get('data', {}))
                plan = json.loads(base64.b64decode(config['agentEnv']['NEMOCLAW_MESSAGING_PLAN_B64']))
                self.assertEqual(plan['channels'], [{'channelId': 'slack', 'active': True}])
                self.assertEqual(config['slack']['responseStyle'], 'incident-summary')
                if runtime == 'openclaw':
                    policy = next(yaml.safe_load(d['data']['policy.yaml']) for d in docs
                                  if d and 'policy.yaml' in d.get('data', {}))
                    filesystem = policy['filesystem_policy']
                    self.assertIn('/opt/nemoclaw-blueprint/model-specific-setup', filesystem['read_only'])
                    self.assertNotIn('/opt', filesystem['read_only'])
                    self.assertNotIn('/opt/nemoclaw-blueprint/model-specific-setup', filesystem['read_write'])
                    self.assertFalse(plan['runtimeSetup']['nodePreloads'][0]['optional'])
                    self.assertEqual(plan['runtimeSetup']['secretScans'][0]['exitCode'], 78)


if __name__ == '__main__':
    unittest.main()
