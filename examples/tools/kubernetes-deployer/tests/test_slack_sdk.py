# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Header-only credential mediation for the pinned Slack SDK compatibility edit."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

CHART = Path(__file__).resolve().parents[1]


def preparation():
    spec = importlib.util.spec_from_file_location('prepare_slack_sdk', CHART / 'files/scripts/prepare_agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SlackSDKTest(unittest.TestCase):
    def fixture(self, root, version='8.0.0'):
        m = preparation()
        package = root / 'npm/projects/openclaw-slack-test/node_modules/@openclaw/slack/node_modules/@slack/web-api'
        (package / 'dist').mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'version': version}))
        source = '''class Client {
    async apiCall(method, options = {}) {
        const headers = {};
        if (options.token) headers.Authorization = `Bearer ${options.token}`;
        const url = method;
''' + m.SLACK_SDK_REQUEST + '''
        return response;
    }
    async makeRequest(url, body, headers) {
        return {url, body: JSON.parse(JSON.stringify(body)), headers};
    }
}
'''
        path = package / 'dist/WebClient.js'
        path.write_text(source)
        return m, path, source

    def test_per_call_token_stays_in_header_and_other_arguments_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            m, path, original = self.fixture(state)
            with mock.patch.object(m, 'SLACK_SDK_SHA256', hashlib.sha256(original.encode()).hexdigest()):
                m.patch_openclaw_slack_sdk(state)
                patched = path.read_text()
                m.patch_openclaw_slack_sdk(state)
                self.assertEqual(path.read_text(), patched)
            checks = '''
const assert = require('node:assert/strict');
(async () => {
  const client = new Client();
  for (const method of ['auth.test', 'chat.postMessage']) {
    const options = {token: 'xoxb-OPENSHELL-RESOLVE-ENV-v12_SLACK_BOT_TOKEN', channel: 'C123', text: 'test'};
    const before = JSON.stringify(options);
    const result = await client.apiCall(method, options);
    assert.equal(result.headers.Authorization, `Bearer ${options.token}`);
    assert.equal(Object.hasOwn(result.body, 'token'), false);
    assert.equal(result.body.channel, options.channel);
    assert.equal(result.body.text, options.text);
    assert.equal(JSON.stringify(options), before);
  }
  assert.deepEqual((await client.apiCall('auth.test')).headers, {});
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
            result = subprocess.run(['node', '-e', patched + checks], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unreviewed_source_or_version_fails_without_modification(self):
        for version in ['8.0.0', '99.0.0']:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                m, path, original = self.fixture(state, version)
                with self.assertRaises(ValueError):
                    m.patch_openclaw_slack_sdk(state)
                self.assertEqual(path.read_text(), original)

    def test_missing_package_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            m = preparation()
            with self.assertRaises(ValueError):
                m.patch_openclaw_slack_sdk(state)
            m, path, original = self.fixture(state)
            outside = state / 'outside.js'
            outside.write_text(original)
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(ValueError):
                m.patch_openclaw_slack_sdk(state)
            self.assertEqual(outside.read_text(), original)


if __name__ == '__main__':
    unittest.main()
