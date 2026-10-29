#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Provision release-owned APF identities and sign the exact rendered policy.

Requires PyYAML, cryptography, authenticated kubectl, and Docker (locally or on
--compiler-host). Registry credentials are separate, existing imagePullSecrets.
Private material travels only through captured output/stdin and compiler tmpfs.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import tarfile
from urllib.parse import urlsplit

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class BootstrapError(Exception):
    pass


def execute(command, *, input=None):
    result = subprocess.run(command, input=input, capture_output=True, timeout=180)
    if result.returncode:
        # Tool errors can contain rendered Secret bodies; do not echo stderr.
        raise BootstrapError(f'{Path(command[0]).name} failed (exit {result.returncode}); no private output logged')
    return result.stdout


def private_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def authority(name, now):
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name[:64])])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def leaf(ca_key, ca_cert, name, now, *, names=(), role=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    fields = [x509.NameAttribute(NameOID.COMMON_NAME, name[:64])]
    if role:
        fields.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role))
    builder = (x509.CertificateBuilder().subject_name(x509.Name(fields)).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if names else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False))
    if names:
        sans = []
        for name in names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                sans.append(x509.DNSName(name))
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    else:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    return {'ca.crt': ca_cert.public_bytes(serialization.Encoding.PEM),
            'tls.crt': builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM),
            'tls.key': private_key(key)}


def verify_external_tls(data, hostname, now):
    """Verify an operator's actual chain, SAN, validity and matching private key."""
    from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
    try:
        chain = x509.load_pem_x509_certificates(data['tls.crt'])
        roots = x509.load_pem_x509_certificates(data['ca.crt'])
        key = serialization.load_pem_private_key(data['tls.key'], password=None)
        if key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != chain[0].public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise BootstrapError('external TLS private key does not match its certificate')
        policy = PolicyBuilder().store(Store(roots)).time(now)
        verifier = policy.build_server_verifier(x509.DNSName(hostname)) if hostname else policy.build_client_verifier()
        verifier.verify(chain[0], chain[1:])
    except (KeyError, ValueError, TypeError, IndexError, VerificationError):
        raise BootstrapError('external TLS chain, hostname, validity or key validation failed') from None


def external_identity(cfg, get, now):
    def data(name):
        obj = get('secret', name)
        if not obj:
            raise BootstrapError('required external identity Secret is missing: ' + name)
        return {k: base64.b64decode(v) for k, v in obj['data'].items()}

    gateway = data(cfg['gatewayTlsSecret'])
    client = data(cfg['gatewayClientTlsSecret'])
    interceptor = data(cfg['apfName'] + '-interceptor-tls')
    jwt = data(cfg['gatewayJwtSecret'])
    verify_external_tls(gateway, cfg['gatewayName'] + '.' + cfg['namespace'] + '.svc.cluster.local', now)
    verify_external_tls(client, None, now)
    verify_external_tls(interceptor, cfg['apfName'] + '.' + cfg['namespace'] + '.svc.cluster.local', now)
    if data(cfg['gatewayClientCaSecret'])['ca.crt'] != client['ca.crt']:
        raise BootstrapError('external gateway client CA differs from the client identity trust root')
    if not isinstance(serialization.load_pem_public_key(jwt['public.pem']), ed25519.Ed25519PublicKey):
        raise BootstrapError('external gateway JWT identity must use Ed25519')
    return {'gateway-ca.crt': gateway['ca.crt'], 'apf-ca.crt': interceptor['ca.crt'],
            'jwt.pub': jwt['public.pem'], 'jwt.kid': jwt['kid']}


def document_inputs(runtime):
    """Translate native names once, before signing. Never mutate a signed body."""
    policy = yaml.safe_load(runtime['policy.yaml'])
    allowed = {'version', 'filesystem_policy', 'landlock', 'process', 'network_policies'}
    if set(policy) - allowed or policy.get('version') != 1:
        raise BootstrapError('unsupported effective native policy; refusing lossy conversion')
    config = json.loads(runtime['bootstrap-config.json'])
    driver = config['driverConfig']
    if set(driver) != {'kubernetes'}:
        raise BootstrapError('only the Kubernetes driver is supported')
    inner = driver['kubernetes']
    claims = {v['name']: v['persistent_volume_claim'] for v in inner['volumes']}
    mounts = [{'type': 'volume', 'source_prefix': claims[m['name']]['claim_name'],
               'target': m['mount_path'],
               'read_only': bool(m.get('read_only') or claims[m['name']].get('read_only'))}
              for m in inner['containers']['agent']['volume_mounts']]
    spec = {'filesystem': policy['filesystem_policy'], 'landlock': policy['landlock'],
            'process': policy['process'], 'network_policies': policy['network_policies'],
            'driver_config': {'deny_templates': True, 'allowed': [{
                'require_role': 'openshell-admin', 'drivers': ['kubernetes'],
                'max_mounts': len(mounts), 'mounts': mounts,
                'kubernetes_config_json': json.dumps(inner, separators=(',', ':'), sort_keys=True)}]}}
    sandbox = {'apiVersion': 'nvidia.agent-policy-fabric.sandbox/v1alpha1',
               'kind': 'SandboxPolicy', 'metadata': {'name': 'default'}, 'spec': spec}
    endpoint = urlsplit(config['model']['baseUrl'])
    if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password:
        raise BootstrapError('APF inference provider requires a credential-free HTTPS URL')
    if config['model']['providerType'] != 'openai':
        raise BootstrapError('APF bootstrap currently requires the native openai provider profile')
    path = endpoint.path.rstrip('/')
    provider = {'apiVersion': 'nvidia.agent-policy-fabric.policy/v1alpha1', 'kind': 'Provider',
                'metadata': {'name': 'openai'}, 'spec': {'type': 'inference', 'endpoints': [{
                    'host': endpoint.hostname, 'port': endpoint.port or 443, 'protocol': 'rest',
                    'enforcement': 'enforce', 'rules': [
                        {'allow': {'method': method, 'path': path + suffix}}
                        for method, suffix in [('POST', '/chat/completions'), ('POST', '/completions'),
                                               ('POST', '/responses'), ('GET', '/models')]]}],
                    'credentials': [{'name': 'api_key', 'envVars': ['OPENAI_API_KEY'],
                                     'required': True, 'style': 'bearer', 'header': 'Authorization',
                                     'source': {'kind': 'host-env', 'name': 'OPENAI_API_KEY'}}]}}
    files = {'sandbox/default-policy.yaml': yaml.safe_dump(sandbox, sort_keys=False).encode(),
             'providers/openai.yaml': yaml.safe_dump(provider, sort_keys=False).encode()}
    if config.get('slack', {}).get('enabled'):
        profile = yaml.safe_load(runtime['slack-provider.yaml'])
        files['providers/slack.yaml'] = yaml.safe_dump(profile, sort_keys=False).encode()
    return files


def compile_bundle(files, owner_seed, name, revision, image, compiler_host, now):
    manifest = {'apiVersion': 'nvidia.agent-policy-fabric.policy/v1alpha1',
                'kind': 'PolicyBundle', 'metadata': {}, 'id': name, 'policy_revision': revision,
                'not_before': (now - dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'expires_at': (now + dt.timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'manifest': [{'path': p, 'sha256': 'REPLACE_WITH_DIGEST'} for p in files]}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        content = {'bundle/' + p: b for p, b in files.items()}
        content['bundle/manifest.template.yaml'] = yaml.safe_dump(manifest, sort_keys=False).encode()
        content['owner.seed'] = owner_seed
        for path, data in content.items():
            info = tarfile.TarInfo(path)
            info.mode, info.size = 0o600, len(data)
            tar.addfile(info, io.BytesIO(data))
    script = ('umask 077; tar xzf - -C /work; '
              '/usr/local/bin/apf-compile genfixture --template /work/bundle/manifest.template.yaml '
              '--out /work/bundle/manifest.yaml --base-dir /work/bundle '
              '--key /work/owner.seed --keyid ' + shlex.quote(name) +
              ' >&2; rm /work/bundle/manifest.template.yaml; tar -C /work -czf - bundle')
    command = ['docker', 'run', '--rm', '-i', '--user', '0', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--tmpfs', '/work:rw,nosuid,nodev,noexec,mode=0700',
               '--entrypoint', '/bin/sh', image, '-ec', script]
    if compiler_host:
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', compiler_host,
                   shlex.join(command)]
    output = execute(command, input=archive.getvalue())
    if len(output) > 700_000:
        raise BootstrapError('signed bundle exceeds conservative ConfigMap size limit')
    with tarfile.open(fileobj=io.BytesIO(output), mode='r:gz') as tar:
        # YamlSigil appends a detached signature document. Inspect only the
        # manifest; the policy service verifies the complete, unchanged stream.
        signed = next(yaml.load_all(tar.extractfile('bundle/manifest.yaml').read(), Loader=yaml.BaseLoader))
    if int(signed.get('policy_revision', 0)) != revision:
        raise BootstrapError('compiler returned an unexpected bundle revision')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifests', type=Path, required=True, help='Helm rendered YAML; never committed')
    parser.add_argument('--context', required=True)
    parser.add_argument('--compiler-image', required=True, help='Compatible digest-pinned policy-service image')
    parser.add_argument('--compiler-host', help='Optional trusted SSH host running Docker')
    args = parser.parse_args()
    if '@sha256:' not in args.compiler_image:
        raise BootstrapError('compiler image must be immutable')
    docs = [d for d in yaml.safe_load_all(args.manifests.read_text()) if isinstance(d, dict)]
    inputs = [d for d in docs if d.get('kind') == 'ConfigMap' and 'bootstrap.json' in d.get('data', {})]
    runtime = [d for d in docs if d.get('kind') == 'ConfigMap' and 'policy.yaml' in d.get('data', {})]
    if len(inputs) != 1 or len(runtime) != 1:
        raise BootstrapError('expected exactly one APF input and one effective runtime ConfigMap')
    cfg = json.loads(inputs[0]['data']['bootstrap.json'])
    namespace, name = cfg['namespace'], cfg['apfName']
    command = ['kubectl', '--context', args.context, '-n', namespace]

    def get(kind, resource):
        data = execute(command + ['get', kind, resource, '--ignore-not-found', '-o', 'json'])
        return json.loads(data) if data.strip() else None

    def create(kind, resource, data, *, binary=False):
        old = get(kind, resource)
        field = 'data' if kind == 'Secret' or not binary else 'binaryData'
        encoded = {k: base64.b64encode(v).decode() for k, v in data.items()} if kind == 'Secret' or binary else data
        if old:
            if old.get('metadata', {}).get('labels', {}).get('apf.nvidia.com/owner') != name:
                raise BootstrapError('refusing to claim a preexisting identity or policy object: ' + resource)
            if old.get(field) != encoded:
                raise BootstrapError('refusing implicit identity rotation: ' + resource)
            return
        obj = {'apiVersion': 'v1', 'kind': kind,
               'metadata': {'name': resource, 'namespace': namespace,
                            'labels': {'apf.nvidia.com/owner': name}}, field: encoded}
        execute(command + ['create', '-f', '-'], input=json.dumps(obj).encode())

    now = dt.datetime.now(dt.timezone.utc)
    files = document_inputs(runtime[0]['data'])
    digest = hashlib.sha256(json.dumps({p: hashlib.sha256(b).hexdigest() for p, b in files.items()},
                                      sort_keys=True).encode()).hexdigest()
    previous = get('configmap', name + '-bundle')
    reuse_bundle = False
    if previous:
        if previous['metadata'].get('labels', {}).get('apf.nvidia.com/owner') != name:
            raise BootstrapError('existing bundle belongs to another owner')
        annotations = previous['metadata'].get('annotations', {})
        old_revision = int(annotations.get('apf.nvidia.com/revision', '0'))
        if cfg['revision'] < old_revision or (cfg['revision'] == old_revision and annotations.get('apf.nvidia.com/input-sha256') != digest):
            raise BootstrapError('changed or older policy requires a strictly greater APF revision; counters are never reset')
        reuse_bundle = cfg['revision'] == old_revision
        if reuse_bundle:
            archive = base64.b64decode(previous['binaryData']['bundle.tar.gz'])
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
                manifest = next(yaml.load_all(tar.extractfile('bundle/manifest.yaml').read(), Loader=yaml.BaseLoader))
            if dt.datetime.fromisoformat(manifest['expires_at']) <= now + dt.timedelta(hours=1):
                raise BootstrapError('bundle expires soon; advance the revision and re-sign, never reset counters')
    external = external_identity(cfg, get, now) if cfg['tlsMode'] == 'existingSecret' else None
    identity = get('secret', name + '-bootstrap-identity')
    if identity:
        if identity['metadata'].get('labels', {}).get('apf.nvidia.com/owner') != name:
            raise BootstrapError('bootstrap identity belongs to another release')
        state = {k: base64.b64decode(v) for k, v in identity['data'].items()}
        if external and any(state.get(k) != v for k, v in external.items()):
            raise BootstrapError('external identity changed; explicit coordinated rotation required')
    elif external:
        state = {**external,
                 'owner.seed': b'ed25519:' + base64.b64encode(secrets.token_bytes(32)) + b'\n',
                 'document-signing.seed': b'ed25519:' + base64.b64encode(secrets.token_bytes(32)) + b'\n',
                 'token': secrets.token_urlsafe(32).encode()}
        create('Secret', name + '-bootstrap-identity', state)
    else:
        gateway_ca_key, gateway_ca = authority(name + ' gateway CA', now)
        apf_ca_key, apf_ca = authority(name + ' interceptor CA', now)
        jwt = ed25519.Ed25519PrivateKey.generate()
        state = {'gateway-ca.key': private_key(gateway_ca_key), 'gateway-ca.crt': gateway_ca.public_bytes(serialization.Encoding.PEM),
                 'apf-ca.key': private_key(apf_ca_key), 'apf-ca.crt': apf_ca.public_bytes(serialization.Encoding.PEM),
                 'jwt.key': private_key(jwt), 'jwt.pub': jwt.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
                 'jwt.kid': secrets.token_hex(16).encode(),
                 'owner.seed': b'ed25519:' + base64.b64encode(secrets.token_bytes(32)) + b'\n',
                 'document-signing.seed': b'ed25519:' + base64.b64encode(secrets.token_bytes(32)) + b'\n',
                 'token': secrets.token_urlsafe(32).encode()}
        # Certificates are generated once and retained verbatim across retries.
        gateway_dns = [cfg['gatewayName'], cfg['gatewayName'] + '.' + namespace,
                       cfg['gatewayName'] + '.' + namespace + '.svc',
                       cfg['gatewayName'] + '.' + namespace + '.svc.cluster.local',
                       'localhost', '127.0.0.1', 'host.openshell.internal']
        for prefix, values in [
            ('gateway', leaf(gateway_ca_key, gateway_ca, cfg['gatewayName'], now, names=gateway_dns)),
            ('client', leaf(gateway_ca_key, gateway_ca, name + '-client', now, role='openshell-user')),
            ('interceptor', leaf(apf_ca_key, apf_ca, name, now, names=[name + '.' + namespace + '.svc.cluster.local'])),
        ]:
            state.update({prefix + '.' + k: v for k, v in values.items()})
        create('Secret', name + '-bootstrap-identity', state)
    # Existing issuer keys never rotate merely because helm upgrade was run.
    if not external:
        for prefix, secret_name in [('gateway', cfg['gatewayTlsSecret']), ('client', cfg['gatewayClientTlsSecret'])]:
            create('Secret', secret_name, {k: state[prefix + '.' + k] for k in ['ca.crt', 'tls.crt', 'tls.key']})
        create('Secret', cfg['gatewayClientCaSecret'], {'ca.crt': state['gateway-ca.crt']})
        create('Secret', cfg['gatewayJwtSecret'], {'signing.pem': state['jwt.key'], 'public.pem': state['jwt.pub'], 'kid': state['jwt.kid']})
    trust = '\n'.join(['issuer = ' + json.dumps('openshell-gateway:' + cfg['gatewayId']),
                       'audience = "urn:openshell:extension:interceptor:apf/policy-adapter"',
                       'kid = ' + json.dumps(state['jwt.kid'].decode()),
                       "public_key_pem = '''" + state['jwt.pub'].decode() + "'''",
                       'max_lifetime_secs = 3600', 'leeway_secs = 30', ''])
    if not external:
        create('Secret', name + '-interceptor-tls', {k: state['interceptor.' + k] for k in ['ca.crt', 'tls.crt', 'tls.key']})
    create('ConfigMap', name + '-extension-trust', {'trust.toml': trust})
    create('Secret', name + '-signing', {k: state[k] for k in ['document-signing.seed', 'token']})
    create('ConfigMap', name + '-ca', {'ca.crt': state['apf-ca.crt'].decode()})
    create('ConfigMap', name + '-gateway-ca', {'ca.crt': state['gateway-ca.crt'].decode()})
    seed = base64.b64decode(state['owner.seed'].decode().strip().removeprefix('ed25519:'))
    public = ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    create('ConfigMap', name + '-trust', {name + '.pub': 'ed25519:' + base64.b64encode(public).decode() + '\n'})
    document_seed = base64.b64decode(state['document-signing.seed'].decode().strip().removeprefix('ed25519:'))
    document_public = ed25519.Ed25519PrivateKey.from_private_bytes(document_seed).public_key()
    create('ConfigMap', name + '-document-key', {'public.pem': document_public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()})
    if reuse_bundle:
        print(json.dumps({'namespace': namespace, 'apf': name, 'revision': cfg['revision'], 'result': 'preserved'}))
        return
    bundle = compile_bundle(files, state['owner.seed'], name, cfg['revision'], args.compiler_image, args.compiler_host, now)
    obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {
        'name': name + '-bundle', 'namespace': namespace, 'labels': {'apf.nvidia.com/owner': name},
        'annotations': {'apf.nvidia.com/revision': str(cfg['revision']), 'apf.nvidia.com/input-sha256': digest}},
        'binaryData': {'bundle.tar.gz': base64.b64encode(bundle).decode()}}
    if previous:
        obj['metadata']['resourceVersion'] = previous['metadata']['resourceVersion']
    execute(command + ['replace' if previous else 'create', '-f', '-'], input=json.dumps(obj).encode())
    print(json.dumps({'namespace': namespace, 'apf': name, 'revision': cfg['revision'],
                      'input_sha256': digest, 'bundle_sha256': hashlib.sha256(bundle).hexdigest(), 'result': 'signed'}))


if __name__ == '__main__':
    try:
        main()
    except (BootstrapError, ValueError, KeyError) as error:
        raise SystemExit(str(error))
