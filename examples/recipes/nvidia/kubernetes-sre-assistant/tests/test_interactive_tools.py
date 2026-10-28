# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import tempfile
import unittest
from pathlib import Path
from test_slack_reports import ROOT, load

def preparation():
    return load("sre_prepare_tools", ROOT / "files/reports/prepare_tools.py")

class InteractiveToolsTest(unittest.TestCase):
    def test_sre_exec_path_and_tool_guidance_are_idempotent(self):
        m = preparation()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / '.openclaw/workspace/TOOLS.md'
            path.parent.mkdir(parents=True)
            path.write_text('Existing team tools.\n')
            config = {'tools': {'exec': {'security': 'allowlist', 'pathPrepend': ['/custom', '/chart-bin']}}}
            env = {'SRE_KUBECONFIG': '/sandbox/.hermes/sre-kubeconfig'}
            m.configure(config, home, env)
            first = path.read_text()
            m.configure(config, home, env)
            self.assertEqual(config['tools']['exec']['pathPrepend'], ['/chart-bin', '/custom'])
            self.assertEqual(config['tools']['exec']['security'], 'allowlist')
            self.assertEqual(first, path.read_text())
            self.assertIn('Existing team tools.', first)
            self.assertIn('/chart-bin/oc --kubeconfig=', first)
            self.assertIn('cluster-status.sh', first)
            self.assertNotIn('cluster-admin', first)


    def test_sre_tools_leave_other_recipes_unchanged_and_reject_symlinks(self):
        m = preparation()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = {}
            m.configure(cfg, home, {})
            self.assertEqual(cfg, {})
            self.assertFalse((home / '.openclaw').exists())
            path = home / '.openclaw/workspace/TOOLS.md'
            path.parent.mkdir(parents=True)
            target = home / 'unrelated'
            target.write_text('unchanged')
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                m.configure(cfg, home, {'SRE_KUBECONFIG': '/sandbox/.hermes/sre-kubeconfig'})
            self.assertEqual(target.read_text(), 'unchanged')
