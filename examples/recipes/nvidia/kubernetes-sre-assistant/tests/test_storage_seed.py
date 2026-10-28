# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Feature disablement must remove owned skill state without deleting user skills."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import test_chart as chart_tests


class StorageSeedTests(unittest.TestCase):
    def test_enable_then_disable_preserves_unrelated_user_content(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            skills = state / 'hermes' / 'skills'
            skills.mkdir(parents=True)
            user_skill = skills / 'user-owned'
            user_skill.mkdir()
            (user_skill / 'notes.txt').write_text('operator-owned notes')
            module = chart_tests.RecipeTest().load_plugin('storage_seed_transition', state, Path(directory))
            module.seed.write_owner_marker('seeding')
            payloads = {'kubernetes-sre/SKILL.md': b'core skill\n',
                        'kubernetes-storage/SKILL.md': b'storage skill\n',
                        'kubernetes-storage/scripts/diagnose-storage.py': b'print("fixture")\n'}
            with mock.patch.dict(os.environ, {'SRE_STORAGE_DIAGNOSTICS_ENABLED': 'true'}):
                module.reconcile_sre_skills(False, payloads)
            owned = skills / 'kubernetes-storage'
            self.assertTrue((owned / 'scripts/diagnose-storage.py').is_file())
            self.assertEqual(stat.S_IMODE(owned.stat().st_mode), 0o555)
            with mock.patch.dict(os.environ, {'SRE_STORAGE_DIAGNOSTICS_ENABLED': 'false'}):
                module.reconcile_sre_skills(False, payloads)
            self.assertFalse(owned.exists())
            self.assertTrue((skills / 'kubernetes-sre/SKILL.md').is_file())
            self.assertEqual((user_skill / 'notes.txt').read_text(), 'operator-owned notes')


if __name__ == '__main__':
    unittest.main()
