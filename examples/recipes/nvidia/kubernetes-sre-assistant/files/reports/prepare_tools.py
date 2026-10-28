# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""SRE-owned native tool configuration, mounted read-only by the recipe."""
import os
import re
from pathlib import Path

def private_write(path, data):
    if path.is_symlink():
        raise ValueError('refusing symlink for managed agent configuration')
    temporary = path.with_name(path.name + '.sre-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(data)
    temporary.chmod(0o600)
    temporary.replace(path)

def configure(config, home, environment):
    """Expose chart-installed tools to native exec without changing its permissions."""
    if environment.get('SRE_KUBECONFIG') != '/sandbox/.hermes/sre-kubeconfig':
        return
    execution = config.setdefault('tools', {}).setdefault('exec', {})
    execution['pathPrepend'] = list(dict.fromkeys(['/chart-bin', *execution.get('pathPrepend', [])]))
    path = home / '.openclaw/workspace/TOOLS.md'
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('refusing symlink SRE tool instructions')
    original = path.read_text() if path.exists() else ''
    start, end = '<!-- sre-cluster-tools:start -->', '<!-- sre-cluster-tools:end -->'
    if original.count(start) != original.count(end):
        raise ValueError('incomplete managed SRE tool instructions')
    original = re.sub(re.escape(start) + r'.*?' + re.escape(end), '', original, flags=re.DOTALL).rstrip()
    guidance = '''Cluster access is preconfigured for this agent through the SRE proxy.
Use the native exec tool for live cluster questions. The OpenShift CLI is installed
at /chart-bin/oc (kubectl-compatible); exec PATH includes /chart-bin. The existing
kubeconfig is /sandbox/.hermes/sre-kubeconfig. Do not print it or request credentials.
For current cluster status, run /bin/sh /sandbox/.sre-reports/cluster-status.sh;
it collects GPU/model and storage status. For specific queries use, for example:
/chart-bin/oc --kubeconfig=/sandbox/.hermes/sre-kubeconfig get nodes
If a shell says oc/kubectl is missing, try the absolute CLI path before claiming
cluster access is unavailable. If a query fails, report its actual error.
Gateway status alone is not cluster status. Never substitute stale report data
for a live query. Status and investigation requests are read-only. Existing proxy
permissions and remediation approval rules still apply; do not bypass denials.'''
    content = original + '\n\n' + start + '\n' + guidance + '\n' + end + '\n'
    if not path.exists() or content != path.read_text():
        private_write(path, content)
