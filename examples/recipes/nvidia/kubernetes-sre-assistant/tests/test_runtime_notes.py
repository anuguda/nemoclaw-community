# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Install instructions must reference only the selected runtime's resources."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


CHART = Path(__file__).resolve().parents[1]


class RuntimeNotesTest(unittest.TestCase):
    def render_notes(self, runtime, access_enabled, terminal_enabled=True, upgrade=False):
        # Helm omits NOTES from `template`. Render the actual production NOTES
        # as a named template in a minimal chart, without cluster dependencies.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            templates = root / 'templates'
            templates.mkdir()
            (root / 'Chart.yaml').write_text('apiVersion: v2\nname: notes-test\nversion: 0.1.0\n')
            values = yaml.safe_load((CHART / 'values.yaml').read_text())
            values['deployer'] = {
                'nameOverride': 'deployer',
                'platform': {'openshift': {'enabled': False}},
                'agent': {'runtime': runtime, 'sandbox': {'name': 'test-agent'}},
                'access': {
                    'enabled': access_enabled,
                    'dashboard': {'enabled': True, 'port': 18789},
                    'api': {'enabled': True, 'port': 8642},
                },
                'operatorClient': {'enabled': terminal_enabled},
                'lifecycle': {'seed': {'runOnUpgrade': False}},
            }
            (root / 'values.yaml').write_text(yaml.safe_dump(values))
            shutil.copyfile(CHART / 'templates/_helpers.tpl', templates / '_helpers.tpl')
            notes = (CHART / 'templates/NOTES.txt').read_text()
            (templates / '_notes.tpl').write_text('{{- define "runtime-notes" -}}\n' + notes + '\n{{- end -}}\n')
            (templates / 'probe.yaml').write_text(
                'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes\ndata:\n  notes: |\n'
                '{{ include "runtime-notes" . | indent 4 }}\n')
            command = ['helm', 'template', 'verification', str(root)]
            if upgrade:
                command.append('--is-upgrade')
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return yaml.safe_load(result.stdout)['data']['notes']

    def test_openclaw_does_not_advertise_absent_hermes_relay(self):
        notes = self.render_notes('openclaw', False)
        self.assertIn('OpenClaw', notes)
        self.assertNotIn('port-forward', notes)
        self.assertNotIn('gateway-token', notes)
        self.assertIn('attach -it', notes)

    def test_disabled_hermes_access_and_terminal_do_not_advertise_missing_resources(self):
        notes = self.render_notes('hermes', False, False)
        self.assertNotIn('port-forward', notes)
        self.assertNotIn('gateway-token', notes)
        self.assertNotIn('attach -it', notes)

    def test_enabled_hermes_access_keeps_connection_instructions(self):
        notes = self.render_notes('hermes', True)
        self.assertIn('port-forward', notes)
        self.assertIn('gateway-token', notes)
        self.assertIn('attach -it', notes)

    def test_upgrade_does_not_advertise_a_seed_job_that_did_not_run(self):
        notes = self.render_notes('openclaw', False, upgrade=True)
        self.assertNotIn('logs job/', notes)


if __name__ == '__main__':
    unittest.main()
