#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create a secret-free derived image context for managed NemoClaw Slack.

Targets the NemoClaw v0.0.130 managed capability-union image contract. No
packages are downloaded by the derived image. Build-time probes reject a base
without native Slack dependencies. This command only writes a build context;
building/pushing and cluster validation are separate operator steps.
"""
import argparse
import json
from pathlib import Path
import re


def write_context(directory, runtime, image):
    if runtime not in {'hermes', 'openclaw'} or not re.fullmatch(
            r'ghcr\.io/nvidia/nemoclaw/' + re.escape(runtime) + r'-sandbox@sha256:[a-f0-9]{64}', image):
        raise ValueError('base must be the matching official managed image pinned by digest')
    plan = {'channels': [{'channelId': 'slack', 'active': True}], 'disabledChannels': [],
            'runtimeSetup': {'nodePreloads': [], 'envAliases': [], 'secretScans': []}}
    if runtime == 'openclaw':
        plan['runtimeSetup']['nodePreloads'] = [{
            'channelId': 'slack',
            'source': '/usr/local/lib/nemoclaw/preloads/slack-channel-guard.js',
            'target': '/tmp/nemoclaw-slack-channel-guard.js',
            'injectInto': ['boot', 'connect'], 'optional': False,
        }]
        plan['runtimeSetup']['secretScans'] = [{
            'channelId': 'slack', 'path': '/sandbox/.openclaw/openclaw.json',
            'pattern': '(?:xoxb|xapp)-(?!OPENSHELL-RESOLVE-ENV-)',
            'message': 'Slack credential found in managed configuration', 'exitCode': 78,
        }]
        probe = '''RUN test -x /usr/local/bin/openclaw && test -x /usr/local/bin/node \\
    && test -f /usr/local/lib/nemoclaw/preloads/slack-channel-guard.js \\
    && /usr/bin/python3 -c 'import json' \\
    && node -e 'const fs=require("fs"); const root="/sandbox/.openclaw/npm/projects"; const roots=["/sandbox/.openclaw/extensions/slack", "/usr/local/lib/node_modules/openclaw/dist/extensions/slack", ...(fs.existsSync(root)?fs.readdirSync(root).map(p=>root+"/"+p+"/node_modules/@openclaw/slack"):[])]; if(!roots.some(p => fs.existsSync(p+"/package.json"))) throw Error("base image lacks the managed Slack plugin")'
'''
    else:
        probe = '''RUN test -x /usr/local/bin/hermes \\
    && /opt/hermes/.venv/bin/python -c 'import yaml, slack_bolt, slack_sdk'
'''
    dockerfile = ('# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.\n'
                  '# SPDX-License-Identifier: Apache-2.0\nFROM ' + image + '\nUSER root\n'
                  'RUN test -x /usr/local/bin/nemoclaw-start\n' + probe +
                  'COPY --chown=0:0 --chmod=0444 messaging-runtime-plan.json /usr/local/share/nemoclaw/messaging-runtime-plan.json\n'
                  'USER sandbox\n')
    directory.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier reviewed context.
    for name, data in [('Dockerfile', dockerfile), ('messaging-runtime-plan.json', json.dumps(plan, indent=2)+'\n')]:
        with (directory / name).open('x') as output:
            output.write(data)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', choices=['hermes', 'openclaw'], required=True)
    p.add_argument('--base-image', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    write_context(args.output, args.runtime, args.base_image)
    print('Created secret-free context at ' + str(args.output))
    print('Build for the cluster architecture, push to your registry, then use the resulting digest in deployer.agent.image.multiarch.')


if __name__ == '__main__':
    main()
