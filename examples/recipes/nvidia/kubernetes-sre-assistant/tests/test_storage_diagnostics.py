# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Storage findings must follow exact resource evidence, not capacity guesses."""
import copy
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'files/skills/kubernetes-storage/scripts/diagnose-storage.py'
SPEC = importlib.util.spec_from_file_location('storage_diagnostics', SCRIPT)
assert SPEC is not None and SPEC.loader is not None
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


class StorageDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.claim = {'metadata': {'name': 'data', 'namespace': 'tenant', 'uid': 'new-uid', 'resourceVersion': '42'},
                      'spec': {'storageClassName': 'rbd', 'volumeName': 'pv-data',
                               'resources': {'requests': {'storage': '1Gi'}}},
                      'status': {'phase': 'Bound', 'capacity': {'storage': '1Gi'}}}
        self.sc = {'allowVolumeExpansion': True, 'volumeBindingMode': 'Immediate'}
        self.pv = {'spec': {'claimRef': {'uid': 'new-uid'}}}

    def assess(self, events=None, pods=None, volume=True):
        return storage.claim_assessment(self.claim, self.sc, pods or [], events or [], self.pv if volume else None)

    def warning(self, uid='new-uid'):
        return {'involvedObject': {'kind': 'PersistentVolumeClaim', 'uid': uid},
                'type': 'Warning', 'reason': 'ProvisioningFailed', 'message': 'provisioner is unavailable',
                'lastTimestamp': '2026-10-28T10:00:00Z'}

    def pending(self):
        self.claim['status'] = {'phase': 'Pending'}
        self.claim['spec'].pop('volumeName')

    def test_bound_claim_does_not_turn_capacity_into_usage(self):
        result = self.assess()
        self.assertEqual(result['state'], 'healthy')
        self.assertIsInstance(result['filesystemUsage'], str)
        self.assertNotIn('usedBytes', result)
        self.assertNotIn('usagePercent', result)
        self.assertEqual(result['uid'], 'new-uid')

    def test_pv_bound_to_recreated_claim_is_unsafe(self):
        self.pv['spec']['claimRef']['uid'] = 'old-uid'
        self.assertEqual(self.assess()['state'], 'unhealthy')

    def test_bound_volume_must_be_independently_verified(self):
        self.assertEqual(self.assess(volume=False)['state'], 'unverified')

    def test_pending_delayed_binding_without_consumer_is_waiting(self):
        self.pending()
        self.sc['volumeBindingMode'] = 'WaitForFirstConsumer'
        self.assertEqual(self.assess()['state'], 'waiting')

    def test_pending_warning_for_exact_claim_is_unhealthy(self):
        self.pending()
        self.assertEqual(self.assess(events=[self.warning()])['state'], 'unhealthy')

    def test_old_pvc_warning_does_not_contaminate_new_claim(self):
        self.pending()
        result = self.assess(events=[self.warning('old-uid')])
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['events'], [])

    def test_resolved_warning_does_not_override_bound_state(self):
        self.assertEqual(self.assess(events=[self.warning()])['state'], 'healthy')

    def test_filesystem_resize_pending_is_not_complete(self):
        self.claim['status']['conditions'] = [{'type': 'FileSystemResizePending', 'status': 'True'}]
        self.assertEqual(self.assess()['state'], 'waiting')

    def test_missing_storageclass_cannot_be_green(self):
        result = storage.claim_assessment(self.claim, None, [], [], self.pv)
        self.assertEqual(result['state'], 'unverified')

    def test_consumer_selection_does_not_include_unrelated_pods(self):
        pods = [{'metadata': {'name': 'consumer'}, 'spec': {'volumes': [{'persistentVolumeClaim': {'claimName': 'data'}}]},
                 'status': {'phase': 'Pending'}},
                {'metadata': {'name': 'other'}, 'spec': {'volumes': [{'persistentVolumeClaim': {'claimName': 'elsewhere'}}]}}]
        self.assertEqual(self.assess(pods=pods)['consumers'], [{'name': 'consumer', 'phase': 'Pending'}])

    def test_unknown_ceph_health_is_not_green(self):
        self.assertEqual(storage.ceph_assessment([{'status': {}}])[0]['state'], 'unverified')
        for health in ['HEALTH_WARN', 'HEALTH_ERR']:
            self.assertEqual(storage.ceph_assessment([{'status': {'ceph': {'health': health}}}])[0]['state'], 'unhealthy')

    def test_missing_claim_is_not_an_empty_healthy_collection(self):
        class MissingClaim:
            def get(self, resource, name='', namespace=''):
                if resource == 'persistentvolumeclaims':
                    raise storage.CollectionError('Error from server (NotFound): PVC missing')
                return {'items': []}
        result = storage.collect(MissingClaim(), 'tenant', 'missing', '', 20)
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['errors'][0]['resource'], 'persistentvolumeclaims')

    def test_ceph_denial_is_distinct_from_absent_api(self):
        claim, sc, pv = copy.deepcopy(self.claim), copy.deepcopy(self.sc), copy.deepcopy(self.pv)
        sc['metadata'] = {'name': 'rbd'}
        class Client:
            error = ''
            def get(self, resource, name='', namespace=''):
                if resource == 'persistentvolumeclaims': return claim
                if resource == 'persistentvolumes': return pv
                if resource == 'storageclasses.storage.k8s.io': return {'items': [sc]}
                if resource == 'cephclusters.ceph.rook.io': raise storage.CollectionError(self.error)
                return {'items': []}
        client = Client()
        client.error = 'Error from server (Forbidden): access denied'
        denied = storage.collect(client, 'tenant', 'data', 'openshift-storage', 20)
        self.assertEqual(denied['state'], 'unverified')
        self.assertEqual(denied['cephCollection'], 'unverified')
        client.error = 'the server doesn\'t have a resource type "cephclusters"'
        absent = storage.collect(client, 'tenant', 'data', 'openshift-storage', 20)
        self.assertEqual(absent['state'], 'healthy')
        self.assertEqual(absent['cephCollection'], 'not_applicable')


if __name__ == '__main__':
    unittest.main()
