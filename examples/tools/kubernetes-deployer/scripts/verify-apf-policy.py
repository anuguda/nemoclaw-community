#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Verify an actual official GetSandbox proto3-JSON response before agent work.

Use the document public key from <release>-apf-document-key, never a private
signing seed. TLS/authenticated gRPC collection is an operator responsibility.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def verify(response, public_key, *, sandbox, workspace, key_id, bundle_id, revision, now=None):
    native = response['sandbox']
    metadata = native['metadata']
    if metadata['name'] != sandbox or metadata['workspace'] != workspace:
        raise ValueError('native-sandbox-identity-mismatch')
    token = metadata['annotations']['apf.nvidia.com/provenance-token']
    if not isinstance(token, str) or len(token) > 65536:
        raise ValueError('invalid-provenance-size')
    header, body, signature = token.split('.')
    jwt_header, claims = json.loads(decode(header)), json.loads(decode(body))
    if jwt_header.get('alg') != 'EdDSA' or jwt_header.get('kid') != key_id:
        raise ValueError('unapproved-provenance-key-or-algorithm')
    key = serialization.load_pem_public_key(public_key)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('provenance-key-must-be-ed25519')
    key.verify(decode(signature), (header + '.' + body).encode())
    now = time.time() if now is None else now
    if not isinstance(claims.get('exp'), (int, float)) or claims['exp'] <= now:
        raise ValueError('provenance-expired')
    if not isinstance(claims.get('iat'), (int, float)) or claims['iat'] > now + 30:
        raise ValueError('provenance-issued-in-future')
    if claims.get('apf/version') != revision or claims.get('apf/bundle-id') != bundle_id:
        raise ValueError('unapproved-bundle-identity-or-revision')
    digest = hashlib.sha256(json.dumps({'policy': native['spec']['policy']}, sort_keys=True,
                                      separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if claims.get('apf/spec-digest') != digest:
        raise ValueError('native-policy-differs-from-signed-projection')
    return {'sandbox': sandbox, 'workspace': workspace, 'signature_verified': True,
            'spec_digest_verified': True, 'spec_sha256': digest, 'bundle_id': bundle_id, 'revision': revision}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--sandbox', required=True)
    parser.add_argument('--workspace', default='default')
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--bundle-id', required=True)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args()
    if args.response.stat().st_size > 2 * 1024 * 1024 or args.public_key.stat().st_size > 16384:
        raise ValueError('proof-input-exceeds-limit')
    result = verify(json.loads(args.response.read_text()), args.public_key.read_bytes(),
                    sandbox=args.sandbox, workspace=args.workspace, key_id=args.key_id,
                    bundle_id=args.bundle_id, revision=args.revision)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (InvalidSignature, ValueError, KeyError, TypeError):
        raise SystemExit('APF native policy proof failed; no private response data logged')
