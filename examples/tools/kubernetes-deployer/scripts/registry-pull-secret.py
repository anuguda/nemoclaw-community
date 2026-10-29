#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Create a GHCR pull Secret; PAT enters via prompt/stdin, never argv or Helm."""
import argparse
import base64
import getpass
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--name', default='apf-ghcr-pull')
    parser.add_argument('--username', required=True)
    parser.add_argument('--token-stdin', action='store_true')
    args = parser.parse_args()
    token = sys.stdin.read(4097).strip() if args.token_stdin else getpass.getpass('GHCR read:packages token: ')
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        raise SystemExit('Invalid registry token input; no credential logged')
    auth = {'auths': {'ghcr.io': {'auth': base64.b64encode((args.username + ':' + token).encode()).decode()}}}
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'kubernetes.io/dockerconfigjson',
           'metadata': {'name': args.name, 'namespace': args.namespace},
           'data': {'.dockerconfigjson': base64.b64encode(json.dumps(auth).encode()).decode()}}
    # Create, not apply: a name collision never silently rotates an existing credential.
    result = subprocess.run(['kubectl', '--context', args.context, '-n', args.namespace, 'create', '-f', '-'],
                            input=json.dumps(obj).encode(), capture_output=True, timeout=30)
    if result.returncode:
        raise SystemExit('Pull Secret creation failed; check context, namespace, create permission and name collision. No private output logged.')
    print('Created GHCR pull Secret ' + args.namespace + '/' + args.name)


if __name__ == '__main__':
    main()
