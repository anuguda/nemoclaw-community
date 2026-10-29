# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reviewed llama.cpp CPU recipe; no GPU, Dynamo, HF token or privileged pod."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

import yaml

MODEL = 'Qwen/Qwen2.5-0.5B-Instruct-GGUF'
REVISION = '9217f5db79a29953eb74d5343926648285ec7e67'
FILENAME = 'qwen2.5-0.5b-instruct-q4_k_m.gguf'
MODEL_SHA256 = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
MODEL_BYTES = 491400032
IMAGE = 'ghcr.io/ggml-org/llama.cpp@sha256:01c2bc28d51291536df2ccebe5c6af1cdec9ea9b2bd98ac59556408caf96d3f8'
DOWNLOADER_IMAGE = 'docker.io/library/python:3.13.15-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285'
RECIPE = {'runtime': 'cpu-llama-cpp', 'model': MODEL, 'model-revision': REVISION,
          'model-file': FILENAME, 'model-sha256': MODEL_SHA256, 'image': IMAGE,
          'downloader-image': DOWNLOADER_IMAGE, 'pvc-size': '2Gi',
          'memory-request': '1Gi', 'memory-limit': '2Gi', 'cpu-request': '2', 'cpu-limit': '4'}
OPTIONS = set(RECIPE) | {'namespace', 'release', 'storage-class', 'platform', 'runner'}


def command(args, *, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError('cpu-cluster-command-failed:' + ' '.join(args[:3]))
    return result.stdout


def plan(arguments, runner):
    if arguments.model != MODEL:
        raise ValueError('cpu-model-not-in-reviewed-recipe:' + MODEL)
    with urlopen('https://huggingface.co/api/models/' + MODEL + '/revision/' + REVISION + '?blobs=true', timeout=25) as response:
        metadata = json.load(response)
    artifact = next((f for f in metadata.get('siblings', []) if f['rfilename'] == FILENAME), {})
    if metadata.get('sha') != REVISION or artifact.get('size') != MODEL_BYTES or artifact.get('lfs', {}).get('sha256') != MODEL_SHA256:
        raise ValueError('cpu-model-source-does-not-match-reviewed-recipe')
    storage = json.loads(command(['/chart-bin/oc', 'get', 'storageclass', arguments.storage_class, '-o', 'json']))
    if storage.get('metadata', {}).get('name') != arguments.storage_class:
        raise ValueError('cpu-storage-class-unavailable')
    options = {**RECIPE, 'namespace': arguments.namespace, 'release': arguments.release,
               'storage-class': arguments.storage_class, 'platform': arguments.platform, 'runner': runner}
    result = {'schemaVersion': 2, 'createdAt': int(time.time()), 'options': options,
              'expose': arguments.expose, 'checks': {'modelRunnerServiceAccount': runner, 'sourceBytes': MODEL_BYTES}}
    validate(result, arguments.namespace, runner)
    return result


def validate(value, target, runner):
    if set(value) != {'schemaVersion', 'createdAt', 'options', 'expose', 'checks'} or value['schemaVersion'] != 2:
        raise ValueError('invalid-cpu-plan-schema')
    if type(value['createdAt']) is not int or not 0 <= time.time() - value['createdAt'] <= 1800:
        raise ValueError('plan-expired-replan-and-confirm')
    options = value['options']
    if not isinstance(options, dict) or set(options) != OPTIONS:
        raise ValueError('invalid-cpu-plan-options')
    if any(options[k] != v for k, v in RECIPE.items()):
        raise ValueError('cpu-plan-recipe-mismatch')
    if options['namespace'] != target or options['runner'] != runner:
        raise ValueError('cpu-deployment-target-or-runner-mismatch')
    label = r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?'
    for key, limit, pattern in [('namespace', 63, label), ('release', 40, label),
                                ('storage-class', 253, r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?'),
                                ('runner', 253, r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?')]:
        if not isinstance(options[key], str) or len(options[key]) > limit or not re.fullmatch(pattern, options[key]):
            raise ValueError('invalid-cpu-' + key)
    if options['release'] != 'qwen2-5-0-5b-instruct-gguf':
        raise ValueError('cpu-model-release-mismatch')
    if options['platform'] not in {'kubernetes', 'openshift'} or type(value['expose']) is not bool:
        raise ValueError('invalid-cpu-platform-or-exposure')
    if value['expose'] and options['platform'] != 'openshift':
        raise ValueError('cpu-native-kubernetes-route-not-supported')
    if value['checks'] != {'modelRunnerServiceAccount': runner, 'sourceBytes': MODEL_BYTES}:
        raise ValueError('invalid-cpu-source-checks')
    return options


def resources(value):
    o = value['options']
    name, namespace = o['release'], o['namespace']
    labels = {'app.kubernetes.io/name': name, 'app.kubernetes.io/managed-by': 'nemoclaw-model-skill'}
    meta = {'name': name, 'namespace': namespace, 'labels': labels}
    source_url = f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{FILENAME}'
    download = '''import hashlib,os,pathlib,urllib.request
p=pathlib.Path('/models/model.gguf')
expected=os.environ['MODEL_SHA256']
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
if p.exists() and p.stat().st_size==int(os.environ['MODEL_BYTES']) and digest(p)==expected:raise SystemExit(0)
t=p.with_suffix('.partial')
try:
 with urllib.request.urlopen(os.environ['MODEL_URL'],timeout=60) as r,t.open('wb') as f:
  total=0
  while True:
   b=r.read(1024*1024)
   if not b:break
   total+=len(b)
   if total>int(os.environ['MODEL_BYTES']):raise ValueError('model-download-exceeds-approved-size')
   f.write(b)
 if t.stat().st_size!=int(os.environ['MODEL_BYTES']) or digest(t)!=expected:raise ValueError('model-download-integrity-failed')
 t.replace(p)
finally:
 t.unlink(missing_ok=True)
'''
    security = {'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    pod_security = {'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}}
    if o['platform'] == 'kubernetes':
        pod_security.update(runAsUser=1000, runAsGroup=1000, fsGroup=1000)
    spec = {'serviceAccountName': o['runner'], 'automountServiceAccountToken': False,
            'securityContext': pod_security,
            'initContainers': [{'name': 'model-download', 'image': DOWNLOADER_IMAGE,
                'command': ['/usr/local/bin/python3', '-B', '-c', download], 'securityContext': security,
                'env': [{'name': k, 'value': v} for k, v in {
                    'MODEL_URL': source_url, 'MODEL_SHA256': MODEL_SHA256, 'MODEL_BYTES': str(MODEL_BYTES)}.items()],
                'resources': {'requests': {'cpu': '100m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}},
                'volumeMounts': [{'name': 'model', 'mountPath': '/models'}]}],
            'containers': [{'name': 'model', 'image': IMAGE,
                'args': ['--model', '/models/model.gguf', '--alias', MODEL, '--host', '0.0.0.0', '--port', '8000',
                         '--ctx-size', '2048', '--threads', '4', '--n-gpu-layers', '0'],
                'env': [{'name': 'HOME', 'value': '/tmp'}], 'securityContext': security,
                'resources': {'requests': {'cpu': o['cpu-request'], 'memory': o['memory-request']},
                              'limits': {'cpu': o['cpu-limit'], 'memory': o['memory-limit']}},
                'ports': [{'name': 'http', 'containerPort': 8000}],
                'startupProbe': {'httpGet': {'path': '/health', 'port': 'http'}, 'periodSeconds': 5, 'failureThreshold': 120},
                'readinessProbe': {'httpGet': {'path': '/health', 'port': 'http'}, 'periodSeconds': 5},
                'volumeMounts': [{'name': 'model', 'mountPath': '/models', 'readOnly': True}, {'name': 'tmp', 'mountPath': '/tmp'}]}],
            'volumes': [{'name': 'model', 'persistentVolumeClaim': {'claimName': name}}, {'name': 'tmp', 'emptyDir': {}}]}
    pod_metadata = {'labels': labels}
    if o['platform'] == 'openshift':
        pod_metadata['annotations'] = {'openshift.io/required-scc': 'restricted-v2'}
    items = [{'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': meta,
              'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': o['storage-class'],
                       'resources': {'requests': {'storage': o['pvc-size']}}}},
             {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': meta,
              'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': labels},
                       'template': {'metadata': pod_metadata, 'spec': spec}}},
             {'apiVersion': 'v1', 'kind': 'Service', 'metadata': meta,
              'spec': {'selector': labels, 'ports': [{'name': 'http', 'port': 8000, 'targetPort': 'http'}]}}]
    if value['expose']:
        items.append({'apiVersion': 'route.openshift.io/v1', 'kind': 'Route', 'metadata': meta,
                      'spec': {'to': {'kind': 'Service', 'name': name}, 'port': {'targetPort': 'http'},
                               'tls': {'termination': 'edge', 'insecureEdgeTerminationPolicy': 'Redirect'}}})
    return items


def deployment_ready(value):
    metadata, status = value.get('metadata', {}), value.get('status', {})
    return (value.get('spec', {}).get('replicas') == 1
            and status.get('observedGeneration', 0) >= metadata.get('generation', 1)
            and all(status.get(key, 0) == 1 for key in
                    ['replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas']))


def wait_ready(namespace, name, timeout=420):
    # The bounded API proxy does not stream watch responses.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = json.loads(command(['/chart-bin/oc', '-n', namespace, 'get',
                                    'deployment', name, '-o', 'json']))
        if deployment_ready(value):
            return True
        time.sleep(5)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-json', required=True)
    args = parser.parse_args()
    value = json.loads(args.plan_json)
    root = Path(__file__).resolve().parents[1]
    intake = yaml.safe_load((root / 'hf-token-intake.yaml').read_text())
    target = intake.get('namespace') or yaml.safe_load((root / 'deployment-target.yaml').read_text())['targetNamespace']
    options = validate(value, target, intake['modelRunnerServiceAccount'])
    # Refuse adoption of preexisting resources outside this reviewed skill.
    for resource in ['pvc', 'deployment', 'service'] + (['route'] if value['expose'] else []):
        existing = command(['/chart-bin/oc', '-n', target, 'get', resource, options['release'], '--ignore-not-found', '-o', 'json'])
        if existing.strip():
            labels = json.loads(existing)['metadata'].get('labels', {})
            if labels.get('app.kubernetes.io/managed-by') != 'nemoclaw-model-skill':
                raise ValueError('cpu-resource-name-collision:' + resource)
    command(['/chart-bin/oc', 'apply', '-f', '-'], input=yaml.safe_dump_all(resources(value)), timeout=60)
    try:
        ready = wait_ready(target, options['release'])
    except (ValueError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        print('DEPLOYMENT_RESULT=pending')
        print('DEPLOYMENT_REASON=inspect-existing-cpu-pod-and-download-do-not-retry')
        return 0
    print('DEPLOYMENT_RESULT=ready')
    print('DEPLOYMENT_SERVICE=' + options['release'] + '.' + target + '.svc.cluster.local:8000')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        print('DEPLOYMENT_RESULT=failed')
        print('DEPLOYMENT_REASON=' + str(error))
        raise SystemExit(1)
