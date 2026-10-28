# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Protected interactive remediation must not expose core mutation paths."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import yaml

CHART = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('protected_proxy', CHART / 'files/scripts/api_proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProtectedAdvancedTest(unittest.TestCase):
    def allowed(self, path, method):
        return proxy.request_allowed(path, method, 'protected-advanced')

    def test_named_application_lifecycle(self):
        base = '/apis/apps/v1/namespaces/team-a/deployments'
        for method, path in [('POST', base), ('PATCH', base+'/test'), ('PUT', base+'/test'), ('DELETE', base+'/test')]:
            with self.subTest(method=method):
                self.assertTrue(self.allowed(path, method))
        self.assertTrue(self.allowed(base+'/test/scale', 'PATCH'))
        self.assertFalse(self.allowed(base, 'DELETE'))
        self.assertFalse(self.allowed(base+'/test/scale', 'DELETE'))

    def test_core_namespaces_and_own_control_plane_are_protected(self):
        with patch.dict(os.environ, {'PROXY_PROTECTED_NAMESPACES': 'sre-bot'}):
            for ns in ['default', 'kube-system', 'kube-public', 'openshift', 'openshift-etcd', 'openshift-monitoring', 'etcd', 'trustee-operator-system', 'sre-bot', '%6bube-system', '%256bube-system']:
                for method in ['DELETE', 'PATCH', 'POST', 'PUT']:
                    with self.subTest(namespace=ns, method=method):
                        self.assertFalse(self.allowed(f'/api/v1/namespaces/{ns}/pods/test', method))
                self.assertTrue(self.allowed(f'/api/v1/namespaces/{ns}/pods/test/log', 'GET'))

    def test_cluster_scope_and_dangerous_resources_denied(self):
        paths = ['/api/v1/namespaces/team-a', '/api/v1/nodes/n1', '/api/v1/persistentvolumes/pv1',
                 '/apis/apiextensions.k8s.io/v1/customresourcedefinitions/test',
                 '/apis/rbac.authorization.k8s.io/v1/namespaces/team-a/roles/admin',
                 '/apis/security.openshift.io/v1/securitycontextconstraints/privileged',
                 '/api/v1/namespaces/team-a/serviceaccounts/default/token',
                 '/api/v1/namespaces/team-a/pods/test/exec',
                 '/api/v1/namespaces/team-a/pods/test/eviction',
                 '/api/v1/namespaces/team-a/configmaps/kube-root-ca.crt']
        for path in paths:
            for method in ['POST', 'PATCH', 'PUT', 'DELETE']:
                with self.subTest(path=path, method=method):
                    self.assertFalse(self.allowed(path, method))

    def test_credentials_storage_and_path_bypasses(self):
        for path in ['/api/v1/secrets', '/api/v1/namespaces/team-a/secrets/s', '/api/v1/watch/namespaces/team-a/secrets/s', '/api/v1/namespaces/team-a/%2573ecrets/s']:
            for method in ['GET', 'POST', 'PATCH', 'DELETE']:
                self.assertFalse(self.allowed(path, method))
        base = '/api/v1/namespaces/team-a/persistentvolumeclaims'
        self.assertTrue(self.allowed(base, 'POST'))
        self.assertFalse(self.allowed(base+'/model-cache', 'DELETE'))
        for path in ['/apis/apps/v1/namespaces/team-a%2F..%2Fopenshift-etcd/deployments/test', '/apis/apps/v1/namespaces/team-a/deployments/test/status', '/apis/apps/v1/namespaces/team-a/deployments/test/extra/tail']:
            self.assertFalse(self.allowed(path, 'DELETE'))
        self.assertFalse(proxy.request_allowed('/api/v1/namespaces/team-a/pods/test', 'DELETE', 'general'))

    def test_model_inference_proxy_is_bounded(self):
        base = '/api/v1/namespaces/models/services/http:test:8000/proxy/v1/chat/completions'
        with patch.dict(os.environ, {'PROXY_SERVICE_PROXY_NAMESPACE':'models'}):
            self.assertTrue(self.allowed(base, 'POST'))
            self.assertFalse(self.allowed(base.replace('/models/', '/other/'), 'POST'))
            self.assertFalse(self.allowed(base.replace('chat/completions', 'admin'), 'POST'))

    def test_hf_reference_returns_only_synthetic_metadata(self):
        target = '/api/v1/namespaces/models/secrets/hf'
        with patch.dict(os.environ, {'PROXY_HF_SECRET_NAMESPACE':'models', 'PROXY_HF_SECRET_NAME':'hf'}):
            self.assertTrue(self.allowed(target, 'GET'))
            for path in [target+'?watch=true', target.replace('/namespaces/', '/watch/namespaces/'), target+'/other', target.replace('/hf','/other')]:
                self.assertFalse(self.allowed(path, 'GET'))
            self.assertEqual(json.loads(proxy.secret_existence_response(target)),
                             {'apiVersion':'v1','kind':'Secret','metadata':{'name':'hf','namespace':'models'}})
            # Exercise the handler: even a misbehaving upstream returning a full
            # Secret must never leak it. The body is not read at all.
            from unittest.mock import MagicMock
            handler = object.__new__(proxy.ProxyHandler)
            handler.path, handler.command = target, 'GET'
            handler.headers = {}
            handler.rfile = io.BytesIO()
            handler.send_payload = MagicMock()
            upstream = MagicMock()
            upstream.status = 200
            upstream.__enter__.return_value = upstream
            upstream.read.return_value = b'{"data":{"HF_TOKEN":"sensitive"}}'
            with patch.object(proxy, 'is_authorized', return_value=True), patch.object(proxy, 'read_bounded', return_value='test'), patch.object(proxy.ssl, 'create_default_context'), patch.object(proxy, 'urlopen', return_value=upstream):
                handler.proxy()
            upstream.read.assert_not_called()
            self.assertEqual(handler.send_payload.call_args.args[1], proxy.secret_existence_response(target))

    def test_chart_requires_ack_and_rbac_has_no_cluster_mutations(self):
        args = ['helm', 'template', 'sre', str(CHART), '--api-versions', 'agents.x-k8s.io/v1alpha1', '--set', 'deployer.platform.serviceAccountIssuerDiscovery.preexistingAnonymousAccess=true', '--set', 'global.sre.rbac.mode=protected-advanced']
        r = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('I_ACKNOWLEDGE_PROTECTED_CLUSTER_REMEDIATION', r.stderr)
        r = subprocess.run(args+['--set','global.sre.rbac.dangerousAcknowledgement=I_ACKNOWLEDGE_PROTECTED_CLUSTER_REMEDIATION'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        role = next(d for d in docs if d['kind']=='ClusterRole' and d['metadata']['name'].endswith('-sre'))
        for rule in role['rules']:
            self.assertFalse(set(rule['verbs']) & {'*','deletecollection','escalate','bind','impersonate'})
            if set(rule['verbs']) - {'get','list','watch'}:
                self.assertNotIn('*', rule['resources'])
                self.assertFalse(set(rule['resources']) & {'customresourcedefinitions','namespaces','nodes','secrets','serviceaccounts'})
        d = next(d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='sre-sre-proxy')
        env = {x['name']:x.get('value') for x in d['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['PROXY_POLICY'], 'protected-advanced')
        self.assertIn('DELETE', env['PROXY_ALLOWED_METHODS'])


if __name__ == '__main__':
    unittest.main()
