# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import copy
import importlib.util
from pathlib import Path
import time
import unittest

PATH = Path(__file__).resolve().parents[1] / 'files/skills/openshift-llm-deploy/scripts/cpu_model.py'
spec = importlib.util.spec_from_file_location('cpu_model_recipe', PATH)
cpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpu)


class CpuPlanBoundaryTests(unittest.TestCase):
    def plan(self):
        return {'schemaVersion': 2, 'createdAt': int(time.time()),
                'options': {**cpu.RECIPE, 'namespace': 'models', 'release': 'qwen2-5-0-5b-instruct-gguf',
                            'storage-class': 'local-path', 'platform': 'kubernetes', 'runner': 'model-runner'},
                'expose': False, 'checks': {'modelRunnerServiceAccount': 'model-runner', 'sourceBytes': cpu.MODEL_BYTES}}

    def test_reviewed_plan_cannot_change_image_model_digest_or_privileged_options(self):
        for key, value in [('image', 'attacker.invalid/image:latest'), ('model-sha256', '0' * 64),
                           ('model-revision', 'main'), ('model-file', '../private'),
                           ('downloader-image', 'attacker.invalid/loader:latest'), ('privileged', 'true')]:
            plan = self.plan()
            plan['options'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                cpu.validate(plan, 'models', 'model-runner')

    def test_confirmation_expires_and_cannot_change_target_runner_or_exposure(self):
        original = self.plan()
        cpu.validate(original, 'models', 'model-runner')
        for change in [lambda p: p.update(createdAt=int(time.time()) - 1801),
                       lambda p: p.update(createdAt=int(time.time()) + 60),
                       lambda p: p['options'].update(namespace='another-team'),
                       lambda p: p['options'].update(runner='default'),
                       lambda p: p['options'].update(release='existing-release'),
                       lambda p: p.update(expose=True),
                       lambda p: p['checks'].update(sourceBytes=1)]:
            value = copy.deepcopy(original)
            change(value)
            with self.assertRaises(ValueError):
                cpu.validate(value, 'models', 'model-runner')

    def test_openshift_route_requires_boolean_approved_exposure(self):
        value = self.plan()
        value['options']['platform'] = 'openshift'
        value['expose'] = True
        cpu.validate(value, 'models', 'model-runner')
        value['expose'] = 'false'
        with self.assertRaises(ValueError):
            cpu.validate(value, 'models', 'model-runner')

    def test_readiness_requires_current_generation_and_all_new_replicas(self):
        ready = {'metadata': {'generation': 3}, 'spec': {'replicas': 1},
                 'status': {'observedGeneration': 3, 'replicas': 1,
                            'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}}
        self.assertTrue(cpu.deployment_ready(ready))
        for key, value in [('observedGeneration', 2), ('updatedReplicas', 0),
                           ('readyReplicas', 0), ('availableReplicas', 0), ('replicas', 2)]:
            pending = copy.deepcopy(ready)
            pending['status'][key] = value
            with self.subTest(key=key):
                self.assertFalse(cpu.deployment_ready(pending))


if __name__ == '__main__':
    unittest.main()
