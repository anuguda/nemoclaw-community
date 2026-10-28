#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reconcile only this chart's native OpenClaw report jobs after gateway startup."""
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

OWNER = 'kubernetes-sre-assistant:scheduled-report:v1'
CLI = '/usr/local/bin/openclaw'


def command(*args):
    result = subprocess.run([CLI, 'cron', *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Native scheduler command failed')
    return json.loads(result.stdout)


def reconcile(config):
    ZoneInfo(config['timeZone'])
    listed = command('list', '--all', '--json')
    jobs = listed.get('jobs', [])
    ids = {}
    for mode in ('infra', 'hf'):
        name = 'sre-slack-report-' + mode
        matches = [j for j in jobs if j.get('name') == name]
        if len(matches) > 1 or any(j.get('description') != OWNER for j in matches):
            raise ValueError('Conflicting report job ownership')
        if not config.get('enabled'):
            if matches:
                command('disable', matches[0]['id'])
            continue
        args = ['--name', name, '--description', OWNER,
                '--cron', (config.get('huggingFaceSchedule', '35 9 * * *') if mode == 'hf' else config['schedule']), '--tz', config['timeZone'],
                '--command-argv', json.dumps(['/usr/bin/python3', '/sandbox/.sre-reports/run_report.py', mode, '--deliver']),
                '--command-env', 'SRE_REPORTS_CONFIG=' + json.dumps(config),
                '--command-env', 'KUBECONFIG=/sandbox/.hermes/sre-kubeconfig',
                '--timeout-seconds', '180', '--output-max-bytes', '18000',
                '--no-deliver', '--channel', 'slack', '--to', config['channel'],
                '--session', 'isolated']
        if matches:
            command('edit', matches[0]['id'], *args)
            command('enable', matches[0]['id'])
            ids[mode] = matches[0]['id']
        else:
            result = command('add', *args)
            ids[mode] = result.get('id', result.get('job', {}).get('id'))
    return ids


def main():
    config = json.loads(os.environ['SRE_REPORTS_CONFIG'])
    output = Path('/sandbox/.openclaw/workspace/reports/scheduler-setup.json')
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {'ok': False}
    # The prepare hook runs before the native gateway; wait for its local API.
    for attempt in range(12):
        try:
            ids = reconcile(config)
            result = {'ok': True, 'enabled': bool(config.get('enabled')), 'jobs': ids}
            break
        except Exception as exc:
            result = {'ok': False, 'error': type(exc).__name__}
            time.sleep(5)
    if output.is_symlink():
        raise SystemExit('Refusing symlink scheduler status')
    output.write_text(json.dumps(result) + '\n')
    output.chmod(0o600)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
