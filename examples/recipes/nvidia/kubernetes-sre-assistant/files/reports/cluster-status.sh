#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Read-only model and capacity inventory for the cluster hosting Hermes.
set -eu

# Hermes tool executions do not inherit the container's chart PATH.
export PATH="/chart-bin:/toolbox:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"

usage() {
  cat >&2 <<'EOF'
usage: cluster-status.sh [--namespace NAME]
EOF
  exit 64
}

namespace=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace) namespace=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done

workdir=$(mktemp -d "${TMPDIR:-/tmp}/hermes-cluster-status.XXXXXX") || exit 1
trap 'rm -rf "$workdir"' EXIT HUP INT TERM

oc get nodes -o json >"$workdir/nodes.json"
oc get pods -A -o json >"$workdir/pods.json"
oc get deployments -A -o json >"$workdir/deployments.json"
oc get services -A -o json >"$workdir/services.json"
oc get routes -A -o json >"$workdir/routes.json" 2>/dev/null || printf '{"items": []}\n' >"$workdir/routes.json"
oc get dynamographdeployments.nvidia.com -A -o json >"$workdir/dgd.json" 2>/dev/null || printf '{"items": []}\n' >"$workdir/dgd.json"
oc get sandboxes.agents.x-k8s.io -A -o json >"$workdir/sandboxes.json" 2>/dev/null || printf '{"items": []}\n' >"$workdir/sandboxes.json"
for resource in persistentvolumeclaims persistentvolumes storageclasses.storage.k8s.io cephclusters.ceph.rook.io; do
    oc get "$resource" -A --request-timeout=10s -o json >"$workdir/$resource.json" 2>/dev/null || printf '{"unavailable":true}\n' >"$workdir/$resource.json"
done

python3 - "$workdir/nodes.json" "$workdir/pods.json" "$workdir/deployments.json" "$workdir/services.json" "$workdir/routes.json" "$workdir/dgd.json" "$workdir/sandboxes.json" "$namespace" <<'PY'
import json
import sys
from datetime import datetime, timezone

nodes, pods, deployments, services, routes, dgds, sandboxes = [json.load(open(path, encoding="utf-8")).get("items", []) for path in sys.argv[1:8]]
namespace = sys.argv[8]

def storage_rows():
    from pathlib import Path
    root = Path(sys.argv[1]).parent
    result = []
    def read(resource):
        data = json.loads((root / (resource + '.json')).read_text())
        return None if data.get('unavailable') else data.get('items', [])
    def add(component, status, summary):
        result.append((component, status, ' '.join(str(summary).split())))
    for resource, label, allowed in [('persistentvolumeclaims', 'PVCs', {'Bound'}),
                                      ('persistentvolumes', 'PVs', {'Bound', 'Available', 'Released'})]:
        items = read(resource)
        if items is None:
            add(label, 'UNKNOWN', 'API check unavailable (access or API error)')
            continue
        counts = {}
        bad = []
        for item in items:
            phase = item.get('status', {}).get('phase', 'Unknown')
            counts[phase] = counts.get(phase, 0) + 1
            if phase not in allowed:
                meta = item.get('metadata', {})
                bad.append('/'.join(filter(None, [meta.get('namespace'), meta.get('name')])) + '=' + phase)
        summary = ', '.join(f'{count} {phase}' for phase, count in sorted(counts.items())) or 'none'
        if bad:
            summary += '; ' + ', '.join(bad[:3]) + (f'; +{len(bad)-3} more' if len(bad) > 3 else '')
        add(label, 'ATTENTION' if bad else 'OK', summary)
    classes = read('storageclasses.storage.k8s.io')
    if classes is None:
        add('Storage classes', 'UNKNOWN', 'API check unavailable')
    else:
        defaults = [s['metadata']['name'] for s in classes if any(s['metadata'].get('annotations', {}).get(k) == 'true' for k in ('storageclass.kubernetes.io/is-default-class', 'storageclass.beta.kubernetes.io/is-default-class'))]
        add('Storage classes', 'OK' if len(defaults) == 1 else 'ATTENTION', f'{len(classes)} classes; default: ' + (', '.join(defaults) or 'none'))
    cephs = read('cephclusters.ceph.rook.io')
    if cephs is None:
        add('Ceph', 'UNKNOWN', 'API check unavailable or Ceph is not installed')
    elif not cephs:
        add('Ceph', 'OK', 'No Ceph clusters reported')
    for item in cephs or []:
        ceph = item.get('status', {}).get('ceph', {})
        health = ceph.get('health', 'UNKNOWN')
        capacity = ceph.get('capacity', {})
        level = 'OK' if health == 'HEALTH_OK' else 'ATTENTION'
        summary = health
        try:
            total, used, free = [int(capacity[k]) for k in ('bytesTotal', 'bytesUsed', 'bytesAvailable')]
            if total <= 0 or min(used, free) < 0:
                raise ValueError('invalid capacity')
            summary += f'; raw {used/2**40:.1f}/{total/2**40:.1f} TiB used ({used/total:.0%}), {free/2**40:.1f} TiB free'
        except (KeyError, ValueError, TypeError):
            summary += '; raw capacity unavailable'
            level = 'UNKNOWN' if level == 'OK' else level
        for label, stamp in [('health', ceph.get('lastChecked')), ('capacity', capacity.get('lastUpdated'))]:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
                if age > 900 or age < -60:
                    raise ValueError('stale')
            except (ValueError, TypeError, AttributeError):
                summary += f'; {label} freshness unverified'
                level = 'UNKNOWN' if level == 'OK' else level
        add('Ceph ' + item.get('metadata', {}).get('name', '?'), level, summary)
    pressured, unknown = [], []
    for node in nodes:
        value = next((c.get('status') for c in node.get('status', {}).get('conditions', []) if c.get('type') == 'DiskPressure'), 'Unknown')
        if value != 'False':
            (pressured if value == 'True' else unknown).append(node.get('metadata', {}).get('name', '?'))
    summary = f'{len(pressured)}/{len(nodes)} nodes with DiskPressure'
    if pressured:
        summary += ': ' + ', '.join(pressured)
    if unknown or not nodes:
        summary += f'; {len(unknown)} unknown (node filesystem/inode telemetry not collected)'
    add('Node disks', 'ATTENTION' if pressured else ('UNKNOWN' if unknown or not nodes else 'OK'), summary)
    return result

def quantity(value):
    if value is None or value == "":
        return 0.0
    text = str(value)
    factors = {"m": 0.001, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}
    for suffix, factor in factors.items():
        if text.endswith(suffix):
            try:
                return float(text[:-len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0

def pod_gpu_request(pod):
    spec = pod.get("spec", {}) or {}
    regular = sum(quantity((container.get("resources", {}).get("requests", {}) or {}).get("nvidia.com/gpu")) for container in spec.get("containers", []))
    init = max([quantity((container.get("resources", {}).get("requests", {}) or {}).get("nvidia.com/gpu")) for container in spec.get("initContainers", [])] or [0])
    return max(regular, init)

def model_hint(pod):
    for container in (pod.get("spec", {}) or {}).get("containers", []):
        args = container.get("args", []) or []
        for flag in ("--served-model-name", "--model", "--model-path"):
            if flag in args:
                index = args.index(flag)
                if index + 1 < len(args):
                    return str(args[index + 1])
        for env in container.get("env", []) or []:
            if env.get("name") in {"SERVED_MODEL_NAME", "MODEL_ID", "MODEL_NAME", "MODEL_PATH"} and env.get("value"):
                return str(env["value"])
    return ""

def model_server_candidate(pod):
    metadata = pod.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    images = [container.get("image", "") for container in (pod.get("spec", {}) or {}).get("containers", [])]
    haystack = " ".join([metadata.get("name", ""), *images, *[str(value) for value in labels.values()]]).lower()
    return any(token in haystack for token in ("vllm", "dynamo", "tensorrtllm", "triton", "nim", "inference", "model", "nemotron", "glm", "llm", "sglang", "deepseek", "qwen", "kimi", "minimax", "embed"))

def modelish(value):
    text = str(value or "").lower()
    return any(token in text for token in ("vllm", "dynamo", "tensorrtllm", "triton", "nim", "nemotron", "glm", "llm", "litellm", "sglang", "deepseek", "qwen", "kimi", "minimax", "llama", "mistral", "gemma", "granite", "phi", "embed", "whisper"))

def labels_match(selector, labels):
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())
def uptime(creation_timestamp):
    if not creation_timestamp:
        return "unknown"
    try:
        created = datetime.fromisoformat(creation_timestamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - created
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        if days:
            return "{}d{}h{}m".format(days, hours, minutes)
        if hours:
            return "{}h{}m".format(hours, minutes)
        return "{}m".format(minutes)
    except (ValueError, TypeError):
        return "unknown"

requests = {}
gpu_pods = []
for pod in pods:
    phase = (pod.get("status", {}) or {}).get("phase", "")
    node = (pod.get("spec", {}) or {}).get("nodeName", "")
    gpu = pod_gpu_request(pod)
    if node and phase not in {"Succeeded", "Failed"} and gpu:
        requests[node] = requests.get(node, 0) + gpu
        gpu_pods.append(
            {
                "node": node,
                "namespace": pod.get("metadata", {}).get("namespace", ""),
                "name": pod.get("metadata", {}).get("name", ""),
                "gpu": gpu,
                "phase": phase,
                "model": model_hint(pod),
                "candidate": model_server_candidate(pod),
            }
        )

print("STATUS_RESULT=ok")
print("GPU_CAPACITY_NOTE=FREE is allocatable GPUs minus scheduled GPU requests; it is not GPU-memory telemetry.")
print('STORAGE:')
print('COMPONENT\tSTATUS\tSUMMARY')
for row in storage_rows():
    print('\t'.join(row))
print('CLUSTER_CHECKS:')
print('CHECK\tSTATUS\tDETAIL')
not_ready = [n.get('metadata', {}).get('name', '?') for n in nodes if not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in n.get('status', {}).get('conditions', []))]
print('All nodes\t{}\t{}/{} Ready{}'.format('ATTENTION' if not_ready else 'OK', len(nodes)-len(not_ready), len(nodes), '; ' + ', '.join(not_ready[:5]) if not_ready else ''))
unhealthy = []
for pod in pods:
    status = pod.get('status', {})
    phase = status.get('phase', 'Unknown')
    waiting = [c.get('state', {}).get('waiting', {}).get('reason', '') for c in status.get('containerStatuses', []) + status.get('initContainerStatuses', [])]
    if phase in {'Failed', 'Unknown', 'Pending'} or any(w in {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError'} for w in waiting):
        meta = pod.get('metadata', {})
        unhealthy.append('{}/{} ({})'.format(meta.get('namespace', '?'), meta.get('name', '?'), ', '.join(w for w in waiting if w) or phase))
print('Workload failures\t{}\t{} unhealthy pods{}'.format('ATTENTION' if unhealthy else 'OK', len(unhealthy), '; ' + ', '.join(unhealthy[:4]) if unhealthy else ''))
print("GPU_NODES:")
print("NODE\tREADY\tGPU_CAPACITY\tGPU_ALLOCATABLE\tGPU_REQUESTED\tGPU_FREE\tCPU_ALLOCATABLE\tMEMORY_ALLOCATABLE")
gpu_node_count = 0
for node in sorted(nodes, key=lambda item: item.get("metadata", {}).get("name", "")):
    metadata = node.get("metadata", {}) or {}
    status = node.get("status", {}) or {}
    capacity = status.get("capacity", {}) or {}
    allocatable = status.get("allocatable", {}) or {}
    gpu = quantity(allocatable.get("nvidia.com/gpu"))
    if not gpu:
        continue
    gpu_node_count += 1
    ready = next((condition.get("status", "Unknown") for condition in status.get("conditions", []) if condition.get("type") == "Ready"), "Unknown")
    used = float(requests.get(metadata.get("name", ""), 0))
    free = max(gpu - used, 0.0)
    print("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
        metadata.get("name", ""),
        ready,
        capacity.get("nvidia.com/gpu", "0"),
        allocatable.get("nvidia.com/gpu", "0"),
        int(used) if used.is_integer() else used,
        int(free) if free.is_integer() else free,
        allocatable.get("cpu", "0"),
        allocatable.get("memory", "0"),
    ))
if not gpu_node_count:
    print("No allocatable nvidia.com/gpu nodes were returned.")

print("GPU_WORKLOADS:")
print("NODE\tNAMESPACE\tPOD\tGPU_REQUEST\tPHASE")
for row in sorted(gpu_pods, key=lambda item: (item["node"], item["namespace"], item["name"])):
    gpu = int(row["gpu"]) if row["gpu"].is_integer() else row["gpu"]
    print("{}\t{}\t{}\t{}\t{}".format(row["node"], row["namespace"], row["name"], gpu, row["phase"]))

print("DEPLOYED_MODELS:")
dgd_count = 0
for dgd in sorted(dgds, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = dgd.get("metadata", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    conditions = (dgd.get("status", {}) or {}).get("conditions", []) or []
    summary = ";".join("{}={}:{}".format(c.get("type", ""), c.get("status", ""), c.get("reason", "")) for c in conditions) or "status-pending"
    print("DYNAMO\t{}/{}\t{}".format(metadata.get("namespace", ""), metadata.get("name", ""), summary))
    dgd_count += 1

vllm_count = 0
for deployment in sorted(deployments, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = deployment.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    if labels.get("app.kubernetes.io/name") != "vllm" and labels.get("app.kubernetes.io/managed-by") != "hermes":
        continue
    containers = (((deployment.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {}).get("containers", [])
    args = (containers[0].get("args", []) if containers else [])
    model = "unknown"
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            model = args[index + 1]
            break
    ready = "{}/{}".format((deployment.get("status", {}) or {}).get("readyReplicas", 0), (deployment.get("spec", {}) or {}).get("replicas", 0))
    print("VLLM\t{}/{}\tmodel={}\tready={}".format(metadata.get("namespace", ""), metadata.get("name", ""), model, ready))
    vllm_count += 1
if not dgd_count and not vllm_count:
    print("No direct DynamoGraphDeployment or Hermes/vLLM Deployment resources found.")

candidate_count = 0
for row in sorted(gpu_pods, key=lambda item: (item["namespace"], item["name"])):
    if namespace and row["namespace"] != namespace:
        continue
    if not row["candidate"]:
        continue
    gpu = int(row["gpu"]) if row["gpu"].is_integer() else row["gpu"]
    model = row["model"] or "not-exposed-by-pod-spec"
    print("DISCOVERED_GPU_MODEL\t{}/{}\tmodel={}\tgpus={}\tphase={}".format(row["namespace"], row["name"], model, gpu, row["phase"]))
    candidate_count += 1
if not candidate_count and not dgd_count and not vllm_count:
    print("No GPU model-server candidates were discovered from running pods.")

candidate_pods = [pod for pod in pods if pod_gpu_request(pod) and model_server_candidate(pod)]
known_model_prefixes = set()
for dgd in dgds:
    metadata = dgd.get("metadata", {}) or {}
    if metadata.get("namespace") and metadata.get("name"):
        known_model_prefixes.add((metadata["namespace"], metadata["name"]))
for deployment in deployments:
    metadata = deployment.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    if labels.get("app.kubernetes.io/name") == "vllm" or labels.get("app.kubernetes.io/managed-by") == "hermes":
        if metadata.get("namespace") and metadata.get("name"):
            known_model_prefixes.add((metadata["namespace"], metadata["name"]))

def service_is_model_server(service):
    metadata = service.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    spec = service.get("spec", {}) or {}
    haystack = " ".join([metadata.get("name", ""), *["{}={}".format(key, value) for key, value in labels.items()]])
    if modelish(haystack):
        return True
    for model_namespace, model_name in known_model_prefixes:
        if metadata.get("namespace", "") == model_namespace and (metadata.get("name", "") == model_name or metadata.get("name", "").startswith(model_name + "-")):
            return True
    selector = spec.get("selector", {}) or {}
    for pod in candidate_pods:
        pod_metadata = pod.get("metadata", {}) or {}
        if pod_metadata.get("namespace", "") != metadata.get("namespace", ""):
            continue
        if labels_match(selector, pod_metadata.get("labels", {}) or {}):
            return True
    return False

model_services = {}
for service in services:
    metadata = service.get("metadata", {}) or {}
    if service_is_model_server(service):
        model_services[(metadata.get("namespace", ""), metadata.get("name", ""))] = service

print("MODEL_ROUTES:")
print("KIND\tNAMESPACE/NAME\tBACKEND\tURL")
route_count = 0
for route in sorted(routes, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = route.get("metadata", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    spec = route.get("spec", {}) or {}
    target = (spec.get("to", {}) or {}).get("name", "")
    route_labels = metadata.get("labels", {}) or {}
    route_text = " ".join([metadata.get("name", ""), target, *["{}={}".format(key, value) for key, value in route_labels.items()]])
    if not modelish(route_text) and (metadata.get("namespace", ""), target) not in model_services:
        continue
    host = spec.get("host", "")
    if not host:
        continue
    scheme = "https" if spec.get("tls") else "http"
    print("MODEL_ROUTE\t{}/{}\t{}\t{}://{}".format(metadata.get("namespace", ""), metadata.get("name", ""), target or "unknown", scheme, host))
    route_count += 1
if not route_count:
    print("No model-serving OpenShift Routes found.")

print("MODEL_NODEPORTS:")
print("KIND\tNAMESPACE/NAME\tPORTS")
node_port_count = 0
for service in sorted(services, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = service.get("metadata", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    if (metadata.get("namespace", ""), metadata.get("name", "")) not in model_services:
        continue
    node_ports = []
    for port in (service.get("spec", {}) or {}).get("ports", []):
        if port.get("nodePort"):
            node_ports.append("{}:{}".format(port.get("name") or port.get("port", "port"), port["nodePort"]))
    if not node_ports:
        continue
    print("MODEL_NODEPORT\t{}/{}\t{}".format(metadata.get("namespace", ""), metadata.get("name", ""), ",".join(node_ports)))
    node_port_count += 1
if not node_port_count:
    print("No model-serving NodePort Services found.")

print("SANDBOXES:")
print("NAMESPACE\tNAME\tPHASE\tRUNTIME_CLASS\tUPTIME")
sandbox_count = 0
for sb in sorted(sandboxes, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = sb.get("metadata", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    spec = sb.get("spec", {}) or {}
    status = sb.get("status", {}) or {}
    name = metadata.get("name", "")
    ns = metadata.get("namespace", "")
    phase = status.get("phase", "unknown")
    runtime_class = spec.get("runtimeClassName", "")
    age = uptime(metadata.get("creationTimestamp", ""))
    print("{}\t{}\t{}\t{}\t{}".format(ns, name, phase, runtime_class, age))
    sandbox_count += 1
if not sandbox_count:
    print("No Agent Sandbox resources found.")

print("KATA_PODS:")
print("NAMESPACE\tNAME\tRUNTIME_CLASS\tPHASE\tNODE\tUPTIME")
kata_count = 0
for pod in sorted(pods, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    spec = pod.get("spec", {}) or {}
    runtime_class = spec.get("runtimeClassName", "") or ""
    if "kata" not in runtime_class:
        continue
    metadata = pod.get("metadata", {}) or {}
    if namespace and metadata.get("namespace") != namespace:
        continue
    status = pod.get("status", {}) or {}
    name = metadata.get("name", "")
    ns = metadata.get("namespace", "")
    phase = status.get("phase", "Unknown")
    node = spec.get("nodeName", "")
    age = uptime(metadata.get("creationTimestamp", ""))
    print("{}\t{}\t{}\t{}\t{}\t{}".format(ns, name, runtime_class, phase, node, age))
    kata_count += 1
if not kata_count:
    print("No Kata/confidential-container pods found.")

print("NEMOCLAW:")
print("NAMESPACE\tNAME\tREADY/REPLICAS\tUPTIME")
nemoclaw_count = 0
for deployment in sorted(deployments, key=lambda item: (item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))):
    metadata = deployment.get("metadata", {}) or {}
    name = metadata.get("name", "")
    ns = metadata.get("namespace", "")
    labels = metadata.get("labels", {}) or {}
    haystack = " ".join([name, ns, *["{}={}".format(key, value) for key, value in labels.items()]]).lower()
    if "nemoclaw" not in haystack and "hermes" not in haystack:
        continue
    if namespace and ns != namespace:
        continue
    spec = deployment.get("spec", {}) or {}
    status = deployment.get("status", {}) or {}
    ready = status.get("readyReplicas", 0)
    replicas = spec.get("replicas", 0)
    age = uptime(metadata.get("creationTimestamp", ""))
    print("{}\t{}\t{}/{}\t{}".format(ns, name, ready, replicas, age))
    nemoclaw_count += 1
if not nemoclaw_count:
    print("No Nemoclaw or Hermes deployments found.")
PY
