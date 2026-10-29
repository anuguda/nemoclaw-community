# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Refresh a Pod-bound, multi-audience gateway callback token without logging it."""
from pathlib import Path
import time
from urllib.error import HTTPError, URLError

from bootstrap import atomic_private_bytes, request_admin_token


def main():
    while True:
        try:
            token = request_admin_token()
            # emptyDir root is kubelet-owned; private helper must own its child.
            atomic_private_bytes(Path('/callback-token/identity/token'), token.encode())
        except (HTTPError, URLError, OSError, SystemExit) as error:
            # Retain last identity. Never log request, token or response body.
            code = getattr(error, 'errno', None)
            print(f'APF callback identity refresh failed ({type(error).__name__}, errno={code}); retrying', flush=True)
            time.sleep(10)
            continue
        time.sleep(240)


if __name__ == '__main__':
    main()
