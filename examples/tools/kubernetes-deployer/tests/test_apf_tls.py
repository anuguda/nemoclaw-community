# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generated workload PKI must work with strict verification, not a TLS bypass."""
import datetime
import importlib.util
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts/apf-bootstrap.py'
spec = importlib.util.spec_from_file_location('apf_bootstrap_tls', PATH)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class ApfTlsTests(unittest.TestCase):
    def test_external_tls_rejects_wrong_host_key_and_expiry(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ca_key, ca = bootstrap.authority('external CA', now)
        good = bootstrap.leaf(ca_key, ca, 'apf', now, names=['apf.example.test'])
        client = bootstrap.leaf(ca_key, ca, 'client', now, role='openshell-user')
        bootstrap.verify_external_tls(good, 'apf.example.test', now)
        bootstrap.verify_external_tls(client, None, now)
        expired = bootstrap.leaf(ca_key, ca, 'apf', now - datetime.timedelta(days=31),
                                 names=['apf.example.test'])
        for identity, hostname in [
            (good, 'wrong.example.test'),
            ({**good, 'tls.key': client['tls.key']}, 'apf.example.test'),
            (expired, 'apf.example.test'),
        ]:
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.verify_external_tls(identity, hostname, now)

    def test_strict_client_accepts_only_approved_ca_and_dns(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ca_key, ca = bootstrap.authority('APF regression CA', now)
        identity = bootstrap.leaf(ca_key, ca, 'apf', now, names=['apf.example.test'])
        _, wrong_ca = bootstrap.authority('Untrusted CA', now)
        from cryptography.hazmat.primitives import serialization
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            cert.write_bytes(identity['tls.crt'])
            key.write_bytes(identity['tls.key'])
            key.chmod(0o600)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                listener.settimeout(5)
                failures = []

                def serve():
                    try:
                        for _ in range(3):
                            connection, _ = listener.accept()
                            try:
                                with server_context.wrap_socket(connection, server_side=True) as secured:
                                    secured.sendall(b'authenticated')
                            except ssl.SSLError:
                                connection.close()
                    except Exception as error:
                        failures.append(error)

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                for authority, hostname, succeeds in [
                    (identity['ca.crt'], 'apf.example.test', True),
                    (identity['ca.crt'], 'wrong.example.test', False),
                    (wrong_ca.public_bytes(serialization.Encoding.PEM), 'apf.example.test', False),
                ]:
                    context = ssl.create_default_context(cadata=authority.decode())
                    context.verify_flags |= ssl.VERIFY_X509_STRICT
                    with self.subTest(hostname=hostname, trusted=succeeds):
                        if succeeds:
                            with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                                with context.wrap_socket(raw, server_hostname=hostname) as secured:
                                    self.assertEqual(secured.recv(32), b'authenticated')
                        else:
                            with self.assertRaises(ssl.SSLCertVerificationError):
                                with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                                    with context.wrap_socket(raw, server_hostname=hostname):
                                        pass
                thread.join(timeout=6)
                self.assertFalse(thread.is_alive(), 'TLS listener did not finish all handshakes')
                self.assertEqual(failures, [])


if __name__ == '__main__':
    unittest.main()
