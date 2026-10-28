# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Slack chat release must have an enforced read-only Kubernetes boundary."""
import json
from pathlib import Path
import subprocess
import unittest
import yaml

CHART = Path(__file__).resolve().parents[1]


class SlackRuntimeTest(unittest.TestCase):
    def render(self, *extra):
        return subprocess.run(['helm', 'template', 'sre', str(CHART), '--api-versions',
            'agents.x-k8s.io/v1alpha1', '--set',
            'deployer.platform.serviceAccountIssuerDiscovery.preexistingAnonymousAccess=true', *extra],
            capture_output=True, text=True)

    def test_readonly_rbac_and_proxy_reject_all_mutation_verbs(self):
        r = self.render('--set', 'global.sre.rbac.mode=read-only')
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = list(yaml.safe_load_all(r.stdout))
        role = next(d for d in docs if d and d.get('kind') == 'ClusterRole' and d['metadata']['name'].endswith('-sre'))
        self.assertTrue(all(set(rule['verbs']) <= {'get', 'list', 'watch'} for rule in role['rules']))
        proxy = next(d for d in docs if d and d.get('kind') == 'Deployment' and d['metadata']['name'] == 'sre-sre-proxy')
        env = {v['name']: v.get('value') for v in proxy['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['PROXY_ALLOWED_METHODS'], 'GET')

    def test_slack_chat_cannot_share_mutating_sre_credentials(self):
        r = self.render('--set', 'deployer.agent.slack.enabled=true', '--set', 'deployer.agent.slack.allowedUsers[0]=U123',
            '--set', 'deployer.agent.slack.allowedChannels[0]=C123', '--set', 'deployer.agent.slack.secretRef.name=slack')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Slack chat requires global.sre.rbac.mode=read-only', r.stderr)

    def test_explicit_slack_remediation_preserves_no_delete_boundary(self):
        r = self.render('--set', 'deployer.agent.slack.enabled=true',
                        '--set', 'deployer.agent.slack.allowedUsers[0]=U123',
                        '--set', 'deployer.agent.slack.allowedChannels[0]=C123',
                        '--set', 'deployer.agent.slack.secretRef.name=slack',
                        '--set', 'global.sre.slackRemediation.enabled=true',
                        '--set', 'global.sre.rbac.mode=broad-no-delete',
                        '--set', 'global.sre.rbac.dangerousAcknowledgement=I_ACKNOWLEDGE_CLUSTER_WIDE_NO_DELETE')
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = list(yaml.safe_load_all(r.stdout))
        role = next(d for d in docs if d and d.get('kind') == 'ClusterRole'
                    and d['metadata']['name'].endswith('-sre'))
        for rule in role['rules']:
            self.assertTrue(set(rule['verbs']).isdisjoint({'delete', 'deletecollection', '*', 'bind', 'escalate'}))
        proxy = next(d for d in docs if d and d.get('kind') == 'Deployment'
                     and d['metadata']['name'] == 'sre-sre-proxy')
        env = {v['name']: v.get('value') for v in proxy['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['PROXY_ALLOWED_METHODS'], 'GET,POST,PUT,PATCH')

    def test_slack_remediation_still_requires_broad_access_acknowledgement(self):
        r = self.render('--set', 'global.sre.slackRemediation.enabled=true',
                        '--set', 'global.sre.rbac.mode=broad-no-delete')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('I_ACKNOWLEDGE_CLUSTER_WIDE_NO_DELETE', r.stderr)

    def test_openclaw_skills_are_mounted_under_native_workspace(self):
        r = self.render('--set', 'deployer.agent.runtime=openclaw', '--set', 'deployer.access.enabled=false',
            '--set', 'deployer.agent.image.multiarch=ghcr.io/nvidia/nemoclaw/openclaw-sandbox@sha256:' + 'a'*64)
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = list(yaml.safe_load_all(r.stdout))
        cfg = next(json.loads(d['data']['bootstrap-config.json']) for d in docs if d and 'bootstrap-config.json' in d.get('data', {}))
        mounts = cfg['driverConfig']['kubernetes']['containers']['agent']['volume_mounts']
        self.assertTrue(any(m['mount_path'] == '/sandbox/.openclaw/workspace/skills/kubernetes-sre' for m in mounts))


if __name__ == '__main__':
    unittest.main()
