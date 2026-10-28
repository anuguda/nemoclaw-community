# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from test_slack_reports import ROOT, load


class StorageReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = (ROOT / 'files/reports/cluster-status.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'storage_rows')
        self.env = dict(json=json, datetime=datetime, timezone=timezone,
                        sys=SimpleNamespace(argv=['script', str(self.root / 'nodes.json')]),
                        nodes=[{'metadata': {'name': 'node1'}, 'status': {'conditions': [{'type': 'DiskPressure', 'status': 'False'}]}}])
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<collector>', 'exec'), self.env)
        self.write('persistentvolumeclaims', [{'status': {'phase': 'Bound'}}])
        self.write('persistentvolumes', [{'status': {'phase': 'Bound'}}, {'status': {'phase': 'Released'}}])
        self.write('storageclasses.storage.k8s.io', [{'metadata': {'name': 'ceph-rbd', 'annotations': {'storageclass.kubernetes.io/is-default-class': 'true'}}}])
        self.stamp = datetime.now(timezone.utc).isoformat()
        self.ceph = {'metadata': {'name': 'cluster'}, 'status': {'ceph': {'health': 'HEALTH_OK', 'lastChecked': self.stamp,
                     'capacity': {'bytesTotal': 10*2**40, 'bytesUsed': 3*2**40, 'bytesAvailable': 7*2**40, 'lastUpdated': self.stamp}}}}
        self.write('cephclusters.ceph.rook.io', [self.ceph])
        load('scheduled_report', ROOT / 'files/reports/scheduled_report.py')
        self.renderer = load('storage_report_runner', ROOT / 'files/reports/run_report.py')

    def write(self, name, items):
        (self.root / (name + '.json')).write_text(json.dumps({'items': items}))

    def collect(self):
        return self.env['storage_rows']()

    def test_measured_capacity_and_binding_summary(self):
        rows = self.collect()
        self.assertTrue(all(r[1] == 'OK' for r in rows))
        text = '\n'.join(r[2] for r in rows)
        self.assertIn('raw 3.0/10.0 TiB used (30%), 7.0 TiB free', text)
        self.assertIn('1 Released', text)
        self.assertIn('default: ceph-rbd', text)
        self.assertIn('0/1 nodes with DiskPressure', text)

    def test_unavailable_and_stale_are_explicit(self):
        (self.root / 'persistentvolumeclaims.json').write_text('{"unavailable":true}')
        self.ceph['status']['ceph']['capacity']['lastUpdated'] = '2000-01-01T00:00:00Z'
        self.write('cephclusters.ceph.rook.io', [self.ceph])
        rows = self.collect()
        self.assertEqual(rows[0][1], 'UNKNOWN')
        self.assertTrue(any(r[1] == 'UNKNOWN' and 'freshness unverified' in r[2] for r in rows))

    def test_pending_claim_pressure_and_slack_attention(self):
        self.write('persistentvolumeclaims', [{'metadata': {'namespace': 'apps', 'name': 'data'}, 'status': {'phase': 'Pending'}}])
        self.env['nodes'][0]['status']['conditions'][0]['status'] = 'True'
        rows = self.collect()
        self.assertIn('apps/data=Pending', rows[0][2])
        self.assertEqual(rows[-1][1], 'ATTENTION')
        raw = 'STORAGE:\nCOMPONENT\tSTATUS\tSUMMARY\n' + '\n'.join('\t'.join(r) for r in rows)
        meta, sections = self.renderer.reports._parse_collector_output(raw)
        summary = self.renderer.infra_summary({'metadata': meta, 'sections': sections, 'collector_status': 'ok'}, datetime.now(timezone.utc), {})
        self.assertNotIn('CHECKS PASSED', summary)
        self.assertIn('**Cluster storage**', summary)
        self.assertIn('7.0 TiB free', summary)
        self.assertLess(summary.index('Cluster storage'), summary.index('GPU availability'))
        self.assertNotIn('|', summary)


if __name__ == '__main__':
    unittest.main()
