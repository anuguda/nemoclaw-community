# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Image context must activate only Slack and never accept credentials."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build-slack-image.py'


class SlackImageTest(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('slack_image', SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_context_has_only_slack_no_baked_user_credentials(self):
        m = self.load()
        for runtime in ['hermes', 'openclaw']:
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as folder:
                base = 'ghcr.io/nvidia/nemoclaw/' + runtime + '-sandbox@sha256:' + 'a'*64
                m.write_context(Path(folder), runtime, base)
                plan = json.loads((Path(folder) / 'messaging-runtime-plan.json').read_text())
                self.assertEqual(plan['channels'], [{'channelId': 'slack', 'active': True}])
                dockerfile = (Path(folder) / 'Dockerfile').read_text()
                self.assertIn('FROM ' + base, dockerfile)
                self.assertNotIn('xoxb-', dockerfile)
                self.assertNotIn('SLACK_BOT_TOKEN', dockerfile)
                if runtime == 'openclaw':
                    self.assertEqual(plan['runtimeSetup']['nodePreloads'][0]['optional'], False)

    def test_mutable_or_cross_runtime_base_is_rejected(self):
        m = self.load()
        for image in ['ghcr.io/nvidia/nemoclaw/openclaw-sandbox:latest',
                      'ghcr.io/nvidia/nemoclaw/hermes-sandbox@sha256:' + 'a'*64]:
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
                m.write_context(Path(folder), 'openclaw', image)


if __name__ == '__main__':
    unittest.main()
