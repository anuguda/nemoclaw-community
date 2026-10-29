# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PATH = Path(__file__).resolve().parents[1] / 'scripts/verify-apf-policy.py'
spec = importlib.util.spec_from_file_location('verify_apf_policy', PATH)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class NativePolicyProofTests(unittest.TestCase):
    def test_signed_readback_rejects_policy_signature_identity_and_expiry_changes(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        policy = {'version': 1, 'filesystemPolicy': {'readOnly': ['/usr']}}
        digest = hashlib.sha256(json.dumps({'policy': policy}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        claims = {'iat': 1000, 'exp': 1900, 'apf/version': 3, 'apf/bundle-id': 'release-apf', 'apf/spec-digest': digest}
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
        def token(value):
            message = encode({'alg': 'EdDSA', 'kid': 'release-apf'}) + '.' + encode(value)
            return message + '.' + base64.urlsafe_b64encode(key.sign(message.encode())).rstrip(b'=').decode()
        response = {'sandbox': {'metadata': {'name': 'agent', 'workspace': 'default',
                                             'annotations': {'apf.nvidia.com/provenance-token': token(claims)}},
                                'spec': {'policy': policy}}}
        options = dict(sandbox='agent', workspace='default', key_id='release-apf', bundle_id='release-apf', revision=3, now=1100)
        self.assertTrue(proof.verify(response, public, **options)['spec_digest_verified'])
        mutated = copy.deepcopy(response)
        mutated['sandbox']['spec']['policy']['filesystemPolicy']['readOnly'].append('/private')
        with self.assertRaisesRegex(ValueError, 'differs'):
            proof.verify(mutated, public, **options)
        for updates in [{'exp': 1099}, {'iat': 1200}, {'apf/version': 2}, {'apf/bundle-id': 'other-release'}]:
            changed = copy.deepcopy(response)
            changed['sandbox']['metadata']['annotations']['apf.nvidia.com/provenance-token'] = token({**claims, **updates})
            with self.assertRaises(ValueError):
                proof.verify(changed, public, **options)
        changed = copy.deepcopy(response)
        changed['sandbox']['metadata']['name'] = 'other-agent'
        with self.assertRaises(ValueError):
            proof.verify(changed, public, **options)
        wrong = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        with self.assertRaises(InvalidSignature):
            proof.verify(response, wrong, **options)


if __name__ == '__main__':
    unittest.main()
