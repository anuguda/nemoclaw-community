#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""POST bounded JSON through the existing TLS/RBAC proxy, without chunked framing."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import yaml


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--body', type=Path, required=True)
    args = parser.parse_args()
    for value in [args.namespace, args.service]:
        if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', value):
            raise ValueError('invalid namespace or service')
    path = os.environ.get('KUBECONFIG') or os.environ.get('SRE_KUBECONFIG')
    if not path or os.pathsep in path:
        raise ValueError('one chart-managed kubeconfig is required')
    with Path(path).open('rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('kubeconfig exceeds size limit')
    config = yaml.safe_load(raw)
    selected = next(c['context'] for c in config['contexts'] if c['name'] == config['current-context'])
    cluster = next(c['cluster'] for c in config['clusters'] if c['name'] == selected['cluster'])
    user = next(u['user'] for u in config['users'] if u['name'] == selected['user'])
    server = urlsplit(cluster['server'])
    if (server.scheme != 'https' or not server.hostname or server.username or server.password
            or server.query or server.fragment or server.path not in {'', '/'}
            or cluster.get('insecure-skip-tls-verify')):
        raise ValueError('verified HTTPS proxy configuration is required')
    context = ssl.create_default_context(cadata=base64.b64decode(cluster['certificate-authority-data']).decode('ascii'))
    token = user['token']
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise ValueError('invalid proxy token')
    with args.body.open('rb') as source:
        body = source.read(1048577)
    if len(body) > 1048576 or not isinstance(json.loads(body), dict):
        raise ValueError('bounded JSON object is required')
    target = (cluster['server'].rstrip('/') + f'/api/v1/namespaces/{args.namespace}'
              f'/services/http:{args.service}:8000/proxy/v1/chat/completions')
    request = Request(target, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Content-Length': str(len(body)), 'Accept': 'application/json'})
    # Never redirect a credential-bearing request to another authority.
    with build_opener(HTTPSHandler(context=context), NoRedirect()).open(request, timeout=120) as response:
        result = response.read(1048577)
        if len(result) > 1048576:
            raise ValueError('completion response exceeds size limit')
        json.loads(result)
        sys.stdout.buffer.write(result)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except HTTPError as error:
        raise SystemExit(f'service completion failed with HTTP {error.code}')
    except (OSError, URLError, ValueError, KeyError, StopIteration, TypeError):
        raise SystemExit('service completion failed; proxy credentials and response diagnostics withheld')
