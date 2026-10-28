#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only storage evidence through the chart's existing Kubernetes proxy."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import tempfile

MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 500
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")


class CollectionError(RuntimeError):
    pass


def excerpt(value: object) -> str:
    text = str(value)
    text = re.sub(r"(?i)\b(bearer\s+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    return text[:500]


class Client:
    def __init__(self) -> None:
        self.kubeconfig = os.environ.get("SRE_KUBECONFIG", "")
        if not self.kubeconfig:
            raise CollectionError("SRE_KUBECONFIG is required; alternate credentials are not supported")

    def get(self, resource: str, name: str = "", namespace: str = "") -> dict:
        args = ["/chart-bin/oc", "--kubeconfig", self.kubeconfig,
                "--request-timeout=20s", "get", resource]
        if name:
            args.append(name)
        if namespace:
            args.extend(["--namespace", namespace])
        args.extend(["--output=json", "--chunk-size=100"])
        try:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = subprocess.run(args, stdout=out, stderr=err, timeout=25, check=False)
                out.seek(0)
                payload = out.read(MAX_BYTES + 1)
                err.seek(0)
                error = err.read(4096).decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CollectionError(excerpt(error)) from error
        if process.returncode:
            raise CollectionError(excerpt(error or f"oc exited {process.returncode}"))
        if len(payload) > MAX_BYTES:
            raise CollectionError("API response exceeded 4 MiB; narrow namespace/PVC scope")
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeError) as error:
            raise CollectionError("API returned invalid JSON") from error
        if not isinstance(result, dict):
            raise CollectionError("API returned an invalid resource")
        if "items" in result and (not isinstance(result["items"], list)
                                   or len(result["items"]) > MAX_ITEMS
                                   or not all(isinstance(x, dict) for x in result["items"])):
            raise CollectionError("API list exceeded 500 items or is malformed; narrow scope")
        return result


def items(resource: dict) -> list[dict]:
    if not isinstance(resource.get("items"), list):
        raise CollectionError("API did not return a resource list")
    return resource["items"]


def claim_assessment(pvc: dict, storage_class: dict | None, pods: list[dict],
                     events: list[dict], volume: dict | None) -> dict:
    metadata, spec, status = (pvc.get(key, {}) for key in ("metadata", "spec", "status"))
    uid, name = metadata.get("uid"), metadata.get("name")
    consumers = []
    for pod in pods:
        if any(v.get("persistentVolumeClaim", {}).get("claimName") == name
               for v in pod.get("spec", {}).get("volumes", [])):
            consumers.append({"name": pod.get("metadata", {}).get("name"),
                              "phase": pod.get("status", {}).get("phase", "Unknown")})
    evidence = []
    for event in events:
        target = event.get("involvedObject", {})
        if not uid or target.get("uid") != uid or target.get("kind") != "PersistentVolumeClaim":
            continue
        evidence.append({"reason": event.get("reason"), "type": event.get("type"),
                         "message": excerpt(event.get("message", "")),
                         "lastObserved": event.get("lastTimestamp") or event.get("eventTime")
                         or event.get("metadata", {}).get("creationTimestamp")})
    evidence = sorted(evidence, key=lambda x: x["lastObserved"] or "", reverse=True)[:10]
    phase = status.get("phase", "Unknown")
    state = "healthy" if phase == "Bound" else "unverified"
    findings = []
    if phase == "Lost":
        state = "unhealthy"
        findings.append("PVC reports Lost; preserve data and escalate to storage owner")
    elif phase == "Pending":
        waiting = storage_class and storage_class.get("volumeBindingMode") == "WaitForFirstConsumer"
        if waiting and not consumers:
            state = "waiting"
            findings.append("StorageClass delays binding until a consumer is scheduled")
        elif any(e["type"] == "Warning" for e in evidence):
            state = "unhealthy"
            findings.append("Pending claim has correlated warning events; investigate provisioner/binding")
        else:
            findings.append("Claim is not bound; no verified causal failure yet")
    conditions = [{"type": c.get("type"), "status": c.get("status"),
                   "reason": c.get("reason"), "message": excerpt(c.get("message", ""))}
                  for c in status.get("conditions", [])]
    if any(c["status"] == "True" and c["type"] in {"Resizing", "FileSystemResizePending"}
           for c in conditions):
        if state != "unhealthy":
            state = "waiting"
        findings.append("Resize is incomplete; do not claim guest/filesystem expansion succeeded")
    if phase == "Bound":
        if not spec.get("volumeName"):
            state = "unhealthy"
            findings.append("Bound claim lacks a volumeName")
        elif volume is None:
            if state != "unhealthy":
                state = "unverified"
            findings.append("Bound volume could not be verified")
        elif not uid or volume.get("spec", {}).get("claimRef", {}).get("uid") != uid:
            state = "unhealthy"
            findings.append("PV claimRef UID does not match this PVC; do not alter either resource")
    class_name = spec.get("storageClassName")
    if class_name and storage_class is None:
        if state != "unhealthy":
            state = "unverified"
        findings.append("Referenced StorageClass could not be verified")
    return {"namespace": metadata.get("namespace"), "name": name, "uid": uid,
            "resourceVersion": metadata.get("resourceVersion"), "phase": phase, "state": state,
            "storageClass": class_name, "volume": spec.get("volumeName"),
            "requested": spec.get("resources", {}).get("requests", {}).get("storage"),
            "reportedCapacity": status.get("capacity", {}).get("storage"),
            "allowVolumeExpansion": storage_class.get("allowVolumeExpansion", False) if storage_class else None,
            "filesystemUsage": "unverified: capacity is not filesystem usage",
            "consumers": consumers, "conditions": conditions, "events": evidence, "findings": findings}


def ceph_assessment(clusters: list[dict]) -> list[dict]:
    result = []
    for cluster in clusters:
        status = cluster.get("status", {})
        health = status.get("ceph", {}).get("health")
        state = "healthy" if health == "HEALTH_OK" else "unverified"
        if health in {"HEALTH_WARN", "HEALTH_ERR"}:
            state = "unhealthy"
        if status.get("phase") in {"Error", "Failure"}:
            state = "unhealthy"
        result.append({"name": cluster.get("metadata", {}).get("name"),
                       "namespace": cluster.get("metadata", {}).get("namespace"),
                       "state": state, "health": health, "phase": status.get("phase"),
                       "lastChecked": status.get("ceph", {}).get("lastChecked")})
    return result


def collect(client: Client, namespace: str, pvc_name: str, ceph_namespace: str, maximum: int) -> dict:
    errors = []

    def read(resource: str, name: str = "", scope: str = "") -> dict | None:
        try:
            return client.get(resource, name, scope)
        except CollectionError as error:
            errors.append({"resource": resource, "namespace": scope, "name": name, "error": str(error)})
            return None

    raw = read("persistentvolumeclaims", pvc_name, namespace)
    claims = ([raw] if pvc_name else items(raw)) if raw is not None else []
    if len(claims) > maximum:
        errors.append({"resource": "persistentvolumeclaims", "error": f"Only first {maximum} claims examined; use --pvc"})
        claims = claims[:maximum]
    classes_raw = read("storageclasses.storage.k8s.io")
    classes = {x.get("metadata", {}).get("name"): x for x in items(classes_raw)} if classes_raw is not None else {}
    pods_raw, events_raw = read("pods", scope=namespace), read("events", scope=namespace)
    pods = items(pods_raw) if pods_raw is not None else []
    events = items(events_raw) if events_raw is not None else []
    assessments = []
    for claim in claims:
        spec = claim.get("spec", {})
        volume = read("persistentvolumes", spec["volumeName"]) if spec.get("volumeName") else None
        assessments.append(claim_assessment(claim, classes.get(spec.get("storageClassName")), pods, events, volume))
    ceph, ceph_state = [], "not_requested"
    if ceph_namespace:
        try:
            ceph = ceph_assessment(items(client.get("cephclusters.ceph.rook.io", namespace=ceph_namespace)))
            ceph_state = "collected" if ceph else "not_applicable"
        # Detector descends into handler body; exception type is a single class.
        # pi-lens-ignore: no-boolean-in-except
        except CollectionError as error:
            message = str(error)
            if "doesn't have a resource type" in message or "could not find the requested resource" in message:
                ceph_state = "not_applicable"
            else:
                ceph_state = "unverified"
                errors.append({"resource": "cephclusters.ceph.rook.io", "namespace": ceph_namespace, "error": message})
    states = {x["state"] for x in assessments + ceph}
    state = ("unhealthy" if "unhealthy" in states else "unverified" if errors or "unverified" in states
             else "waiting" if "waiting" in states else "healthy" if assessments else "not_applicable")
    return {"observedAt": datetime.now(timezone.utc).isoformat(), "namespace": namespace,
            "pvc": pvc_name or None, "state": state, "claims": assessments, "ceph": ceph,
            "cephCollection": ceph_state, "errors": errors,
            "scope": "Kubernetes storage metadata only; application I/O, filesystem usage and external telemetry unverified",
            "mutations": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pvc", default="")
    parser.add_argument("--ceph-namespace", default="", help="Explicit optional CephCluster namespace")
    parser.add_argument("--max-pvcs", type=int, default=20)
    args = parser.parse_args()
    if not NAMESPACE.fullmatch(args.namespace) or (args.ceph_namespace and not NAMESPACE.fullmatch(args.ceph_namespace)):
        parser.error("invalid namespace")
    if args.pvc and not NAME.fullmatch(args.pvc):
        parser.error("invalid PVC name")
    if not 1 <= args.max_pvcs <= 100:
        parser.error("--max-pvcs must be between 1 and 100")
    try:
        report = collect(Client(), args.namespace, args.pvc, args.ceph_namespace, args.max_pvcs)
    except CollectionError as error:
        report = {"state": "unverified", "errors": [{"error": str(error)}], "mutations": False}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["state"] == "unhealthy" else 2 if report["state"] == "unverified" else 0


if __name__ == "__main__":
    raise SystemExit(main())
