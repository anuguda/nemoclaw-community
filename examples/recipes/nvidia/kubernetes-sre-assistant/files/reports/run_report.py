#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reuse the canary email collectors; native OpenClaw delivers stdout to Slack."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import subprocess
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo
from typing import Any, Literal, overload

import scheduled_report as reports


def clean(value):
    text = re.sub(r'(?:xox[baprs]|xapp)-[^\s]+', '[REDACTED]', str(value))
    # Neutralize mentions and formatting supplied by remote model/cluster data.
    text = re.sub(r'\s+', ' ', text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').translate(
        str.maketrans({'*': '∗', '`': '′', '|': '¦', '[': '［', ']': '］'}))


def link(url, label):
    parsed = urlsplit(str(url))
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username:
        return clean(label)
    safe_url = quote(str(url), safe=':/?=&%#-_.~')
    return f'[{clean(label)}]({safe_url})'


def timestamp(now, config):
    return now.astimezone(ZoneInfo(config.get('timeZone', 'America/Los_Angeles'))).strftime('%b %d, %Y · %I:%M %p %Z')


def bounded(lines, limit=3500):
    """Keep complete entries and links; never slice a model ID or URL."""
    result = []
    footer = '\n\nMore details are retained in the full report. Ask in this thread for details.'
    for line in lines:
        if len('\n'.join(result + [line])) + len(footer) > limit:
            return '\n'.join(result) + footer
        result.append(line)
    return '\n'.join(result)


def hf_summary(report, now, config):
    models = report['models']
    lines = [f'✅ **Hugging Face · Top {len(models)} trending models**', timestamp(now, config),
             '12B+ parameters · vLLM metadata · base models', '']
    for model in models:
        model_id = str(model['id'])
        url = 'https://huggingface.co/' + quote(model_id, safe='/')
        lines.append(f"{model['rank']}. {link(url, model_id)}\n"
                     f"   {clean(reports._parameter_label(model['parameters']))} parameters · "
                     f"trending {clean(model['trendingScore'])} · released {clean(str(model.get('createdAt') or 'unknown')[:10])}")
    lines.extend(['', link('https://huggingface.co/models?num_parameters=min%3A12B&apps=vllm&base_model_relation=base&sort=trending', 'Browse the source query'),
                  'Discovery is not proof of runtime compatibility. Reply with a model ID to request deployment preflight and a plan for approval.'])
    return bounded(lines)


def infra_summary(snapshot, now, config):
    sections = snapshot['sections']
    def rows(name):
        headers, data = reports._records(sections.get(name, []), reports.SECTION_HEADERS.get(name))
        return [dict(zip(headers, row)) for row in data]
    nodes, models = rows('GPU_NODES'), rows('DEPLOYED_MODELS')
    workloads = rows('GPU_WORKLOADS')
    storage = rows('STORAGE')
    ready_nodes = sum(n.get('Ready') == 'True' for n in nodes)
    def model_ready(model):
        return any(part.startswith('Ready=True') for part in model.get('Status', '').split(';'))
    ready_models = sum(model_ready(m) for m in models)
    failed = snapshot['collector_status'] != 'ok' or snapshot.get('metadata', {}).get('STATUS_RESULT', 'ok') != 'ok'
    troubled = [p for p in workloads if p.get('Phase') not in ('Running', 'Succeeded')]
    state = reports.infrastructure_state(snapshot.get('metadata', {}), sections, snapshot['collector_status'])
    totals = {key: sum(reports._integer(n.get(key, '0')) for n in nodes)
              for key in ('GPU Allocatable', 'GPU Requested', 'GPU Free')}
    marker = '✅' if state == 'CHECKS PASSED' else ':alert:'
    lines = [f'{marker} **TME inference · {state}**', timestamp(now, config),
             f"GPU nodes: **{ready_nodes}/{len(nodes)} ready** · Model resources: **{ready_models}/{len(models)} ready**",
             f"GPUs: **{totals['GPU Requested']}/{totals['GPU Allocatable']} requested** · **{totals['GPU Free']} available**",
             'Available = allocatable minus requests; not GPU-memory telemetry. Readiness is not an inference test.', '']
    if failed:
        lines.append('⚠ Collection failed; counts may be incomplete. Ask for collector diagnostics.')
    issues = [f"• Node {clean(n.get('Node'))}: Ready={clean(n.get('Ready'))}" for n in nodes if n.get('Ready') != 'True']
    issues += [f"• {clean(m.get('Namespace/Name'))}: {clean(m.get('Status'))}" for m in models if not model_ready(m)]
    issues += [f"• {clean(p.get('Namespace'))}/{clean(p.get('Pod'))}: {clean(p.get('Phase'))}" for p in troubled]
    issues += [f"• {clean(c.get('Check'))}: {clean(c.get('Detail'))}" for c in rows('CLUSTER_CHECKS') if c.get('Status') != 'OK']
    if issues:
        lines += ['**Needs attention**', *issues, '']
    lines += ['**Cluster storage**']
    if not storage:
        lines.append('• Storage status unavailable in this collector output.')
    for item in storage:
        marker = '⚠ ' if item.get('Status') != 'OK' else ''
        lines.append(f"• {marker}{clean(item.get('Component', 'Storage'))}: {clean(item.get('Summary', 'unknown'))}")
    lines += ['Raw Ceph capacity includes replication overhead; it is not PVC filesystem free space.', '']
    lines += ['**GPU availability by node**']
    for node in nodes:
        name = str(node.get('Node', '?'))
        lines.append(f"• {clean(name)}: **{clean(node.get('GPU Free', '?'))}/{clean(node.get('GPU Allocatable', '?'))} available** · {'ready' if node.get('Ready') == 'True' else 'not ready'}")
    lines += ['', '**Model resources**']
    for model in models:
        lines.append(f"• {clean(model.get('Namespace/Name', '?'))} · {'ready' if model_ready(model) else 'needs attention'}")
    routes = rows('MODEL_ROUTES')
    if routes:
        lines += ['', '**Model endpoints**', ' · '.join(link(r.get('URL', ''), r.get('Namespace/name', 'endpoint').split('/')[-1]) for r in routes)]
    lines += ['', 'Ask in this thread for workload details, node investigation, or a proposed fix.']
    return bounded(lines)


def presentation(mode, snapshot, summary):
    if mode == 'hf':
        headers = ['Rank', 'Model', 'Parameters', 'Trending', 'Released']
        rows = [[m['rank'], clean(m['id']), reports._parameter_label(m['parameters']), str(m['trendingScore']), str(m.get('createdAt') or 'unknown')[:10]] for m in snapshot['models']]
        links = ' · '.join(link('https://huggingface.co/' + quote(str(m['id']), safe='/'), str(m['rank'])) for m in snapshot['models'])
        note = 'Model links: ' + links + '\n12B+ base models with vLLM metadata. Discovery does not certify runtime compatibility. Reply with a model ID for deployment preflight.'
        caption = 'Hugging Face trending models'
    else:
        headers, rows = ['Check', 'Status', 'Details'], []
        for name in ('CLUSTER_CHECKS', 'STORAGE'):
            _, data = reports._records(snapshot['sections'].get(name, []))
            rows.extend([[clean(r[0]), '✅' if r[1] == 'OK' else '🚨', clean(r[2])[:800]] for r in data])
        nh, nodes = reports._records(snapshot['sections'].get('GPU_NODES', []))
        ni = {h:i for i,h in enumerate(nh)}
        for node in nodes:
            rows.append([clean(node[ni['Node']]), '✅' if node[ni['Ready']] == 'True' else '🚨', f"{node[ni['GPU Free']]}/{node[ni['GPU Allocatable']]} GPUs available"])
        _, models = reports._records(snapshot['sections'].get('DEPLOYED_MODELS', []), reports.SECTION_HEADERS['DEPLOYED_MODELS'])
        rows.extend([[clean(m[1]), '✅' if any(c.startswith('Ready=True') for c in m[2].split(';')) else '🚨', clean(m[2])[:300]] for m in models])
        note = 'Available GPUs = allocatable minus requests, not GPU-memory telemetry. Ceph values are raw capacity, not PVC filesystem free space. Status covers collected checks, not a model inference test.'
        caption = 'Infrastructure checks'
    blocks = []
    if rows:
        selected, size = [], sum(len(h) for h in headers)
        for row in rows[:30]:
            row = [cell[:350] if isinstance(cell,str) else cell for cell in row]
            cost = sum(len(str(cell)) for cell in row)
            if size + cost > 8500:
                break
            selected.append(row)
            size += cost
        blocks.append({'type':'table', 'caption':caption, 'headers':headers, 'rows':selected})
    blocks.append({'type':'context','text':note})
    return {'tone':'success' if summary.startswith('✅') else 'warning','blocks':blocks}


def deliver_slack(summary, view, config):
    channel = config.get('channel', '')
    if not re.fullmatch(r'[CG][A-Z0-9]+', channel):
        raise ValueError('Invalid report channel')
    completed = subprocess.run(['/usr/local/bin/openclaw','message','send','--channel','slack','--target',channel,
        '--message','\n'.join(summary.splitlines()[:2]),'--presentation',json.dumps(view),'--json'],
        capture_output=True,text=True,timeout=60)
    if completed.returncode:
        raise RuntimeError('Slack report delivery failed')
    # The sender returns a receipt; do not treat scheduler enqueue as delivery.
    output = completed.stdout
    try:
        data = json.loads(output[output.index('{'):])
    except ValueError:
        raise RuntimeError('Slack sender returned an invalid delivery receipt') from None
    def message_id(value):
        if isinstance(value, dict):
            if value.get('messageId'):
                return str(value['messageId'])
            for child in value.values():
                found = message_id(child)
                if found:
                    return found
        return None
    receipt = message_id(data)
    if not receipt:
        raise RuntimeError('Slack sender returned no message receipt')
    return {'channel':channel,'messageId':receipt,'delivered':True}


@overload
def generate(mode: str, now: datetime, config: dict[str, Any], *, with_presentation: Literal[True]) -> tuple[str, str, dict[str, Any]]: ...


@overload
def generate(mode: str, now: datetime, config: dict[str, Any], *, with_presentation: Literal[False] = False) -> tuple[str, str]: ...


@overload
def generate(mode: str, now: datetime, config: dict[str, Any], *, with_presentation: bool) -> tuple[str, str] | tuple[str, str, dict[str, Any]]: ...


def generate(mode: str, now: datetime, config: dict[str, Any], *, with_presentation: bool = False) -> tuple[str, str] | tuple[str, str, dict[str, Any]]:
    root = Path(__file__).resolve().parent
    reports.CLUSTER_STATUS = root / 'cluster-status.sh'
    os.environ['HERMES_MODEL_DISCOVERY_SCRIPT'] = str(root / 'model_discovery.py')
    os.environ['HERMES_REPORT_HF_TOP_N'] = str(config.get('topN', 10))
    os.environ['HERMES_REPORT_HF_NAMESPACE'] = 'unused'
    os.environ['HERMES_REPORT_HF_CONFIGMAP'] = 'unused'
    os.environ['HERMES_REPORT_HF_WEBUI_BASE_URL'] = ''
    if mode == 'infra':
        snapshot = {}
        subject, body = reports.infra_report(now, snapshot=snapshot)
        summary = infra_summary(snapshot, now, config)
    else:
        # The report remains advisory. Slack does not need the email UI's
        # approval ConfigMap and must not imply that a model was deployed.
        snapshot = {}
        subject, body = reports.hugging_face_report(now, persister=lambda report, _: snapshot.update(report))
        summary = hf_summary(snapshot, now, config)
    body = re.sub(r'(?:xox[baprs]|xapp)-[^\s]+', '[REDACTED]', body)
    body = body.replace('<!channel>', '[channel]').replace('<!here>', '[here]')
    if with_presentation:
        return summary, body, presentation(mode, snapshot, summary)
    return summary, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['infra', 'hf'])
    parser.add_argument('--deliver', action='store_true')
    parser.add_argument('--preview-presentation', action='store_true')
    args = parser.parse_args()
    try:
        config = json.loads(os.environ.get('SRE_REPORTS_CONFIG', '{}'))
    except ValueError:
        parser.error('SRE_REPORTS_CONFIG must contain a JSON object')
    if not isinstance(config, dict):
        parser.error('SRE_REPORTS_CONFIG must contain a JSON object')
    if not config.get('enabled'):
        raise SystemExit('Scheduled SRE reports are disabled')
    now = datetime.now(timezone.utc)
    try:
        summary, body, view = generate(args.mode, now, config, with_presentation=True)
    except Exception as exc:
        # A failed collector must produce an explicit report rather than
        # stale "healthy" data or silent scheduled-job output.
        summary = f':alert: SRE {args.mode} report: collection failed ({type(exc).__name__}).\nNo current report is available; investigate the collector.'
        if args.deliver:
            deliver_slack(summary, {'blocks':[{'type':'text','text':'Status unverified. No successful report was collected.'}]}, config)
        print(summary)
        return 1
    root = Path('/sandbox/.openclaw/workspace/reports')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f'{args.mode}-{now:%Y%m%dT%H%M%SZ}.md'
    if path.is_symlink():
        raise SystemExit('Refusing symlink report output')
    with path.open('x') as stream:
        stream.write(body)
    path.chmod(0o600)
    if args.deliver:
        receipt = deliver_slack(summary, view, config)
        evidence = path.with_suffix('.delivery.json')
        with evidence.open('x') as stream:
            json.dump(receipt, stream)
        evidence.chmod(0o600)
    if args.preview_presentation:
        print(json.dumps(view))
        return 0
    print(summary)
    return 0


if __name__ == '__main__':
    sys.exit(main())
