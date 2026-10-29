# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Observe the exact seed Job without keeping its RWO state volume attached."""

from __future__ import annotations

import json
import os
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def seed_ready(job: dict, namespace: str, release: str, name: str) -> bool:
    metadata = job.get("metadata", {})
    if (metadata.get("namespace") != namespace or metadata.get("name") != name
            or metadata.get("labels", {}).get("app.kubernetes.io/instance") != release):
        raise SystemExit("seed Job identity does not match this Helm release")
    conditions = job.get("status", {}).get("conditions", [])
    if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
        raise SystemExit("seed Job failed; inspect its retained Pod logs")
    return any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)


def main() -> None:
    from bootstrap import read_bounded_projected_token

    namespace, release = os.environ["RELEASE_ID"].split("/", 1)
    pending = os.environ["SEED_JOB_NAME"].split(",")
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    url_prefix = (f"https://{host}:{port}/apis/batch/v1/namespaces/"
                  f"{quote(namespace, safe='')}/jobs/")
    context = ssl.create_default_context(cafile=os.environ["KUBERNETES_API_CA"])
    deadline = time.monotonic() + int(os.environ.get("SEED_WAIT_TIMEOUT_SECONDS", "300"))
    while time.monotonic() < deadline:
        name = pending[0]
        url = url_prefix + quote(name, safe="")
        token = read_bounded_projected_token(os.environ["KUBERNETES_API_TOKEN"], "seed observer token")
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urlopen(request, context=context, timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise SystemExit("seed Job API response exceeds size limit")
            try:
                job = json.loads(body)
            except (ValueError, UnicodeDecodeError) as error:
                raise SystemExit("invalid seed Job API response") from error
            if not isinstance(job, dict):
                raise SystemExit("invalid seed Job API object")
            if seed_ready(job, namespace, release, name):
                print(f"seed Job {name} completed")
                pending.pop(0)
                if not pending:
                    return
                continue
        except HTTPError as error:
            if error.code != 404:
                raise SystemExit(f"seed Job observation failed with HTTP {error.code}") from error
        time.sleep(2)
    raise SystemExit(f"timed out waiting for seed Job {name}")


if __name__ == "__main__":
    main()
