# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load report test module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.module = load('sre_report_reconciler', ROOT / 'files/reports/reconcile_reports.py')
        self.config = dict(enabled=True, schedule='30 9 * * *',
                           timeZone='America/Los_Angeles', channel='C123', topN=10)

    def test_creates_deterministic_commands_with_required_delivery(self):
        with patch.object(self.module, 'command', side_effect=[{'jobs': []}, {'id': 'infra'}, {'id': 'hf'}]) as call:
            self.assertEqual(self.module.reconcile(self.config), {'infra': 'infra', 'hf': 'hf'})
        for args in [c.args for c in call.call_args_list[1:]]:
            self.assertIn('--command-argv', args)
            self.assertNotIn('--message', args)
            self.assertNotIn('--best-effort-deliver', args)
            self.assertIn('--no-deliver', args)
            self.assertIn('--deliver', json.loads(args[args.index('--command-argv') + 1]))
            self.assertEqual(json.loads(args[args.index('--command-argv') + 1])[0], '/usr/bin/python3')
            self.assertEqual(args[args.index('--to') + 1], 'C123')

    def test_updates_owned_jobs_and_does_not_duplicate(self):
        jobs = [{'name': 'sre-slack-report-' + m, 'description': self.module.OWNER, 'id': m} for m in ('infra', 'hf')]
        with patch.object(self.module, 'command', side_effect=[{'jobs': jobs}, {}, {}, {}, {}]) as call:
            self.module.reconcile(self.config)
        self.assertEqual([c.args[0] for c in call.call_args_list], ['list', 'edit', 'enable', 'edit', 'enable'])

    def test_distinct_default_and_custom_schedules(self):
        for custom in (None, '5 18 * * 1-5'):
            if custom:
                self.config['huggingFaceSchedule'] = custom
            with patch.object(self.module, 'command', side_effect=[{'jobs': []}, {'id': 'infra'}, {'id': 'hf'}]) as call:
                self.module.reconcile(self.config)
            schedules = [c.args[c.args.index('--cron') + 1] for c in call.call_args_list[1:]]
            self.assertEqual(schedules, ['30 9 * * *', custom or '35 9 * * *'])

    def test_disable_preserves_jobs_and_other_schedules(self):
        self.config['enabled'] = False
        jobs = [{'name': 'sre-slack-report-infra', 'description': self.module.OWNER, 'id': 'owned'}, {'name': 'unrelated', 'id': 'other'}]
        with patch.object(self.module, 'command', side_effect=[{'jobs': jobs}, {}]) as call:
            self.module.reconcile(self.config)
        self.assertEqual(call.call_args_list[-1].args, ('disable', 'owned'))

    def test_foreign_job_name_is_not_overwritten(self):
        with patch.object(self.module, 'command', return_value={'jobs': [{'name': 'sre-slack-report-infra', 'id': 'other'}]}):
            with self.assertRaises(ValueError):
                self.module.reconcile(self.config)

    def test_proxy_rejects_delete_and_privilege_bypass(self):
        proxy = load('slack_report_proxy', ROOT / 'files/scripts/api_proxy.py')
        path = '/apis/apps/v1/namespaces/apps/deployments/demo'
        self.assertTrue(proxy.request_allowed(path, 'PATCH', 'general'))
        self.assertFalse(proxy.request_allowed(path, 'DELETE', 'general'))
        for path in ['/api/v1/namespaces/apps/secrets',
                     '/api/v1/namespaces/apps/pods/demo/exec',
                     '/apis/rbac.authorization.k8s.io/v1/clusterrolebindings',
                     '/api/v1/namespaces/apps/serviceaccounts/default/token']:
            self.assertFalse(proxy.request_allowed(path, 'POST', 'general'), path)


class SlackFormatTest(unittest.TestCase):
    def setUp(self):
        load('scheduled_report', ROOT / 'files/reports/scheduled_report.py')
        self.module = load('slack_report_runner', ROOT / 'files/reports/run_report.py')
        self.now = datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)
        self.config = {'timeZone': 'America/Los_Angeles'}

    def test_hf_links_metadata_and_no_email_instructions(self):
        models = [dict(id='nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16',
                       rank=i, parameters=120_000_000_000, trendingScore=57,
                       createdAt='2026-10-12T15:32:06Z') for i in range(1, 11)]
        text = self.module.hf_summary({'models': models}, self.now, self.config)
        self.assertIn('10. [nvidia/', text)
        self.assertIn('120.0B parameters', text)
        self.assertIn('09:30 AM PDT', text)
        self.assertIn('https://huggingface.co/nvidia/', text)
        self.assertNotIn('email-only', text)
        self.assertNotIn('WebUI', text)
        self.assertNotIn('|', text)
        self.assertLessEqual(len(text), 3500)

    def snapshot(self, status='ok') -> dict[str, Any]:
        return dict(collector_status=status, metadata={}, sections={
            'STORAGE': ['COMPONENT\tSTATUS\tSUMMARY', 'PVCs\tOK\t3 Bound'],
            'CLUSTER_CHECKS': ['CHECK\tSTATUS\tDETAIL', 'All nodes\tOK\t1/1 Ready'],
            'GPU_NODES': ['NODE\tREADY\tGPU_ALLOCATABLE\tGPU_REQUESTED\tGPU_FREE',
                          'gpu-worker-01\tTrue\t8\t2\t6'],
            'DEPLOYED_MODELS': ['DYNAMO\tmodel-workloads/demo\tProgressing=False;Ready=True:Ready'],
            'MODEL_ROUTES': ['KIND\tNAMESPACE/NAME\tBACKEND\tURL',
                             'MODEL_ROUTE\tmodel-workloads/demo\tfrontend\thttps://demo.example.com']})

    def test_infra_readiness_links_and_gpu_summary(self):
        text = self.module.infra_summary(self.snapshot(), self.now, self.config)
        self.assertIn('CHECKS PASSED', text)
        self.assertTrue(text.startswith('✅'))
        self.assertIn('**6/8 available**', text)
        self.assertIn('[demo](https://demo.example.com)', text)
        self.assertIn('1/1 ready', text)
        self.assertIn('not GPU-memory telemetry', text)
        self.assertNotIn('|', text)

    def test_failed_missing_and_unhealthy_data_are_not_healthy(self):
        snapshot = self.snapshot('failed')
        self.assertIn('COLLECTION FAILED', self.module.infra_summary(snapshot, self.now, self.config))
        snapshot = dict(collector_status='ok', metadata={}, sections={})
        self.assertIn('STATUS UNVERIFIED', self.module.infra_summary(snapshot, self.now, self.config))
        snapshot = self.snapshot()
        snapshot['sections']['DEPLOYED_MODELS'] = ['DYNAMO\tmodel-workloads/demo\tReady=False:OOMKilled']
        text = self.module.infra_summary(snapshot, self.now, self.config)
        self.assertIn('ATTENTION', text)
        self.assertLess(text.index('Needs attention'), text.index('GPU availability'))
        self.assertIn('OOMKilled', text)

    def test_native_table_and_delivery_receipt(self):
        snapshot = self.snapshot()
        summary = self.module.infra_summary(snapshot, self.now, self.config)
        view = self.module.presentation('infra', snapshot, summary)
        table = view['blocks'][0]
        self.assertEqual(table['type'], 'table')
        self.assertEqual(table['headers'], ['Check','Status','Details'])
        self.assertIn(['PVCs', '✅', '3 Bound'], table['rows'])
        from types import SimpleNamespace
        with patch.object(self.module.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout='{"payload":{"messageId":"123.45"}}')) as call:
            receipt = self.module.deliver_slack(summary, view, {'channel':'C123'})
        self.assertTrue(receipt['delivered'])
        self.assertIn('--presentation', call.call_args.args[0])
        self.assertEqual(receipt['messageId'], '123.45')

    def test_missing_storage_and_current_workload_failures_cannot_be_green(self):
        snapshot = self.snapshot()
        snapshot['sections']['CLUSTER_CHECKS'].append('Workload failures\tATTENTION\t1 unhealthy pod')
        text = self.module.infra_summary(snapshot,self.now,self.config)
        self.assertTrue(text.startswith(':alert:'))
        snapshot = self.snapshot()
        del snapshot['sections']['STORAGE']
        self.assertIn('STATUS UNVERIFIED',self.module.infra_summary(snapshot,self.now,self.config))

    def test_delivery_failure_and_missing_receipt_are_not_success(self):
        from types import SimpleNamespace
        for result in [SimpleNamespace(returncode=1, stdout=''),
                       SimpleNamespace(returncode=0, stdout='{}'),
                       SimpleNamespace(returncode=0, stdout='not JSON'),
                       SimpleNamespace(returncode=0, stdout='{"payload":')]:
            with patch.object(self.module.subprocess,'run',return_value=result), self.assertRaises(RuntimeError):
                self.module.deliver_slack('test',{'blocks':[]},{'channel':'C123'})

    def test_invalid_config_exits_without_echoing_sensitive_input(self):
        sentinel = 'sensitive-config-canary'
        for raw in [f'["{sentinel}"]', f'{{"value":"{sentinel}"']:
            with self.subTest(config=raw):
                env = {**os.environ, 'SRE_REPORTS_CONFIG': raw}
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'files/reports/run_report.py'), 'infra'],
                    env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_untrusted_mentions_links_and_whole_entry_truncation(self):
        text = self.module.clean('<!channel> <@U123> xoxb-test-secret')
        self.assertNotIn('<!', text)
        self.assertNotIn('<@', text)
        self.assertNotIn('xoxb-', text)
        self.assertEqual(self.module.link('javascript:alert(1)', 'model'), 'model')
        text = self.module.bounded(['intro', 'x' * 4000, 'end'])
        self.assertLessEqual(len(text), 3500)
        self.assertNotIn('xxx', text)
        self.assertIn('full report', text)


if __name__ == '__main__':
    unittest.main()
