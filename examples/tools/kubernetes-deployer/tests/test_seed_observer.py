# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import copy
import importlib.util
from pathlib import Path
import unittest

_spec = importlib.util.spec_from_file_location("wait_seed", Path(__file__).parents[1] / "files/scripts/wait_seed.py")
observer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(observer)


class SeedObserverTests(unittest.TestCase):
    def test_only_current_release_success_allows_bootstrap(self):
        job = {"metadata": {"namespace": "agents", "name": "agent-seed-7",
                            "labels": {"app.kubernetes.io/instance": "agent"}},
               "status": {"conditions": []}}
        self.assertFalse(observer.seed_ready(job, "agents", "agent", "agent-seed-7"))
        job["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
        self.assertTrue(observer.seed_ready(job, "agents", "agent", "agent-seed-7"))
        for field, value in [("namespace", "other"), ("name", "agent-seed-6"),
                             ("labels", {"app.kubernetes.io/instance": "other"})]:
            bad = copy.deepcopy(job)
            bad["metadata"][field] = value
            with self.subTest(field=field), self.assertRaises(SystemExit):
                observer.seed_ready(bad, "agents", "agent", "agent-seed-7")
        job["status"]["conditions"].append({"type": "Failed", "status": "True"})
        with self.assertRaises(SystemExit):
            observer.seed_ready(job, "agents", "agent", "agent-seed-7")


if __name__ == "__main__":
    unittest.main()
