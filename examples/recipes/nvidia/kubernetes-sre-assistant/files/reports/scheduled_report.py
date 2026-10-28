#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run one chart-seeded, timezone-aware report and optionally email it."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


HERMES_HOME = Path(os.environ.get("HOME", "/sandbox/.hermes"))
EMAIL_HELPER = HERMES_HOME / "skills/email-report/scripts/send-report-email.py"
CLUSTER_STATUS = HERMES_HOME / "skills/openshift-llm-deploy/scripts/cluster-status.sh"
DEFAULT_MODEL_DISCOVERY = Path("/chart-bin/model-discovery.py")


def fail(message: str) -> None:
    raise RuntimeError(message)


def mode_from_program() -> str:
    name = Path(sys.argv[0]).name
    if "huggingface-discovery" in name:
        return "hf"
    if "model-availability" in name:
        return "model"
    if "infra-status" in name:
        return "infra"
    return ""


def daily_schedule(value: str) -> tuple[int, int]:
    fields = value.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        fail("seeded report schedule must be a daily five-field cron expression")
    try:
        minute, hour = (int(fields[0], 10), int(fields[1], 10))
    except ValueError as exc:
        fail("seeded report minute and hour must be integers")
        raise AssertionError from exc
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        fail("seeded report schedule is outside valid daily bounds")
    return minute, hour


def due(mode: str, now: datetime) -> tuple[bool, str]:
    prefix = {"model": "MODEL", "hf": "HF", "infra": "INFRA"}[mode]
    zone_name = os.environ[f"HERMES_REPORT_{prefix}_TIME_ZONE"]
    minute, hour = daily_schedule(os.environ[f"HERMES_REPORT_{prefix}_SCHEDULE"])
    local = now.astimezone(ZoneInfo(zone_name))
    # Hermes invokes this script hourly at the configured minute. Matching the local hour
    # and a date marker preserves DST behavior and prevents duplicate mail after
    # gateway restarts or delayed ticks.
    return local.hour == hour and local.minute == minute, local.date().isoformat()


def model_report(now: datetime) -> tuple[str, str]:
    base_url = os.environ["HERMES_REPORT_MODEL_BASE_URL"].rstrip("/")
    expected_model = os.environ["HERMES_REPORT_MODEL_ID"]
    request = Request(base_url + "/models", headers={"Accept": "application/json"})
    status = "unavailable"
    detail = ""
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            fail("model endpoint response exceeds 1 MiB")
        payload = json.loads(body)
        models = payload.get("data", []) if isinstance(payload, dict) else []
        match = next(
            (item for item in models if isinstance(item, dict) and item.get("id") == expected_model),
            None,
        )
        if match is None:
            detail = f"Expected model was not advertised; returned IDs: {[item.get('id') for item in models if isinstance(item, dict)]}"
        else:
            status = "available"
            detail = (
                f"Model: {expected_model}\n"
                f"Context window: {match.get('context_window', 'not reported')}\n"
                f"Owner: {match.get('owned_by', 'not reported')}"
            )
    except Exception as exc:
        detail = f"Endpoint check failed: {type(exc).__name__}: {exc}"
    subject = f"[Hermes] Nemotron model availability: {status}"
    report = (
        "# Nemotron model availability\n\n"
        f"Checked UTC: {now.isoformat()}\n"
        f"Endpoint: {base_url}/models\n"
        f"Status: {status}\n\n{detail}\n"
    )
    return subject, report


def _model_discovery_module():
    source = Path(
        os.environ.get("HERMES_MODEL_DISCOVERY_SCRIPT", str(DEFAULT_MODEL_DISCOVERY))
    )
    spec = importlib.util.spec_from_file_location("hermes_native_model_discovery", source)
    if spec is None or spec.loader is None:
        fail(f"cannot load model discovery helper: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _positive_integer(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw, 10)
    except ValueError as exc:
        fail(f"{name} must be an integer")
        raise AssertionError from exc
    if value < 1 or value > maximum:
        fail(f"{name} must be between 1 and {maximum}")
    return value


def _parameter_label(value: object) -> str:
    return f"{int(value) / 1_000_000_000:.1f}B"


def hugging_face_report(now: datetime, *, fetcher=None, persister=None) -> tuple[str, str]:
    discovery = _model_discovery_module()
    top_n = _positive_integer("HERMES_REPORT_HF_TOP_N", 10, 25)
    timeout = _positive_integer("HERMES_REPORT_HF_TIMEOUT_SECONDS", 30, 120)
    config = discovery.DiscoveryConfig(
        namespace=os.environ["HERMES_REPORT_HF_NAMESPACE"],
        report_configmap=os.environ["HERMES_REPORT_HF_CONFIGMAP"],
        webui_base_url=os.environ["HERMES_REPORT_HF_WEBUI_BASE_URL"],
        smtp_host="",
        smtp_port=None,
        email_from="",
        email_to=(),
        top_n=top_n,
        request_timeout_seconds=timeout,
    )
    fetch = discovery.fetch_models if fetcher is None else fetcher
    persist = discovery.persist_report if persister is None else persister
    payload = fetch(top_n, timeout)
    models = discovery.normalize_models(payload, limit=top_n)
    report = discovery.build_report(models, now=now)
    persist(report, config)

    rows = [
        "# ✅ Hugging Face model discovery completed",
        "",
        (
            f"Generated {report['generatedAt']}. Filters: at least 12B parameters, "
            "vLLM, base models, sorted by trending score."
        ),
        "",
        "| Rank | Model | Parameters | Trending | Released | Hermes |",
        "| ---: | --- | ---: | ---: | --- | --- |",
    ]
    for model in models:
        model_id = str(model["id"])
        review_url = discovery.approval_url(
            config.webui_base_url, str(report["reportId"]), model_id
        )
        rows.append(
            "| "
            + " | ".join(
                (
                    str(model["rank"]),
                    f"[{model_id}]({model['url']})",
                    _parameter_label(model["parameters"]),
                    str(model["trendingScore"]),
                    str(model["createdAt"] or "unknown"),
                    f"[Review & approve]({review_url})" if review_url else "email-only (no WebUI)",
                )
            )
            + " |"
        )
    rows.extend(
        [
            "",
            (
                "**Safety:** opening a link never deploys anything. After sign-in, a "
                "deliberate approval button starts Hermes' existing guarded deployment "
                "workflow, including final confirmation before apply."
            ),
            "",
            f"[View the source query on Hugging Face]({report['sourceUrl']})",
            "",
        ]
    )
    return f"✅ Hugging Face discovery: top {len(models)} trending models", "\n".join(rows)


SECTION_TITLES = {
    "CLUSTER_CHECKS": "Cluster health checks",
    "STORAGE": "Cluster storage",
    "GPU_NODES": "GPU nodes",
    "GPU_WORKLOADS": "GPU workloads",
    "DEPLOYED_MODELS": "Deployed models",
    "MODEL_ROUTES": "Model routes",
    "MODEL_NODEPORTS": "Model NodePorts",
    "SANDBOXES": "Agent Sandboxes",
    "KATA_PODS": "Kata and confidential-container pods",
    "NEMOCLAW": "NemoClaw services",
}
SECTION_HEADERS = {
    "DEPLOYED_MODELS": ["Kind", "Namespace/Name", "Status"],
}


def _parse_collector_output(output: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    metadata: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line.endswith(":") and line[:-1] in SECTION_TITLES:
            current = line[:-1]
            sections[current] = []
            continue
        if current is None and "=" in line:
            key, value = line.split("=", 1)
            metadata[key] = value
            continue
        if current is not None and line:
            sections[current].append(line)
    return metadata, sections


def _records(lines: list[str], fallback_headers: list[str] | None = None) -> tuple[list[str], list[list[str]]]:
    if not lines or (len(lines) == 1 and "\t" not in lines[0]):
        return [], []
    rows = [line.split("\t") for line in lines if "\t" in line]
    if not rows:
        return [], []
    if fallback_headers is not None:
        headers = fallback_headers
        data = rows
    else:
        acronyms = {"CPU", "GPU", "ID", "URL"}
        headers = [
            " ".join(
                part if part in acronyms else part.casefold().capitalize()
                for part in item.split("_")
            )
            for item in rows[0]
        ]
        data = rows[1:]
    width = len(headers)
    return headers, [row for row in data if len(row) == width]


def _integer(value: str) -> int:
    try:
        return int(value, 10)
    except ValueError:
        return 0


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _markdown_section(name: str, lines: list[str]) -> str:
    title = SECTION_TITLES[name]
    headers, rows = _records(lines, SECTION_HEADERS.get(name))
    rendered = [f"## {title}", ""]
    if not headers:
        message = lines[0] if lines else "No entries reported."
        rendered.append(message)
        return "\n".join(rendered)
    rendered.append("| " + " | ".join(_markdown_cell(item) for item in headers) + " |")
    rendered.append("| " + " | ".join("---" for _item in headers) + " |")
    rendered.extend(
        "| " + " | ".join(_markdown_cell(item) for item in row) + " |"
        for row in rows
    )
    if not rows:
        rendered.append("| " + " | ".join("No entries" if index == 0 else "" for index in range(len(headers))) + " |")
    return "\n".join(rendered)


def infrastructure_state(metadata, sections, collector_status):
    if collector_status != 'ok' or metadata.get('STATUS_RESULT', 'ok') != 'ok':
        return 'COLLECTION FAILED'
    if any(name not in sections for name in ('GPU_NODES', 'DEPLOYED_MODELS', 'STORAGE', 'CLUSTER_CHECKS')):
        return 'STATUS UNVERIFIED'
    _, nodes = _records(sections['GPU_NODES'])
    _, models = _records(sections['DEPLOYED_MODELS'], SECTION_HEADERS['DEPLOYED_MODELS'])
    _, storage = _records(sections['STORAGE'])
    _, checks = _records(sections['CLUSTER_CHECKS'])
    if not nodes or not storage or not checks:
        return 'STATUS UNVERIFIED'
    if any(row[1] != 'True' for row in nodes) or any(not any(c.startswith('Ready=True') for c in row[-1].split(';')) for row in models):
        return 'NEEDS ATTENTION'
    if any(row[1] != 'OK' for row in storage + checks):
        return 'NEEDS ATTENTION'
    return 'CHECKS PASSED'


def infra_report(now: datetime, *, snapshot=None) -> tuple[str, str]:
    completed = subprocess.run(
        ["/bin/sh", str(CLUSTER_STATUS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    collector_status = "ok" if completed.returncode == 0 else "failed"
    metadata, sections = _parse_collector_output(completed.stdout[:900000])
    if snapshot is not None:
        snapshot.update(metadata=metadata, sections=sections, collector_status=collector_status)
    node_headers, node_rows = _records(sections.get("GPU_NODES", []))
    node_indexes = {name: index for index, name in enumerate(node_headers)}
    ready_nodes = sum(
        1 for row in node_rows if row[node_indexes.get("Ready", -1)] == "True"
    )
    allocatable = sum(
        _integer(row[node_indexes["GPU Allocatable"]])
        for row in node_rows
        if "GPU Allocatable" in node_indexes
    )
    requested = sum(
        _integer(row[node_indexes["GPU Requested"]])
        for row in node_rows
        if "GPU Requested" in node_indexes
    )
    free = sum(
        _integer(row[node_indexes["GPU Free"]])
        for row in node_rows
        if "GPU Free" in node_indexes
    )
    _model_headers, model_rows = _records(
        sections.get("DEPLOYED_MODELS", []), SECTION_HEADERS["DEPLOYED_MODELS"]
    )
    ready_models = sum(1 for row in model_rows if row[-1].startswith("Ready=True"))
    status = infrastructure_state(metadata, sections, collector_status)
    marker = '✅' if status == 'CHECKS PASSED' else '🚨'
    subject = f"{marker} TME inference infrastructure: {status}"
    highlights = [
        f"# {marker} TME inference infrastructure status",
        "",
        f"**Overall status:** {status.upper()}",
        "",
        "## Highlights",
        "",
        f"- Checked UTC: **{now.isoformat()}**",
        f"- Collector status: **{collector_status}**",
        f"- GPU nodes ready: **{ready_nodes}/{len(node_rows)}**",
        f"- GPU requests: **{requested}/{allocatable}**; free allocatable GPUs: **{free}**",
        f"- Model deployments ready: **{ready_models}/{len(model_rows)}**",
    ]
    capacity_note = metadata.get("GPU_CAPACITY_NOTE", "")
    if capacity_note:
        highlights.extend(["", f"> {capacity_note}"])
    report_parts = ["\n".join(highlights)]
    for name in SECTION_TITLES:
        if name in sections:
            report_parts.append(_markdown_section(name, sections[name]))
    if completed.stderr.strip():
        report_parts.extend(
            [
                "## Collector diagnostics",
                "",
                "```text",
                completed.stderr[:65536].rstrip(),
                "```",
            ]
        )
    report = "\n\n".join(report_parts).rstrip() + "\n"
    return subject, report


def deliver(report_path: Path, subject: str, recipients: list[str]) -> None:
    base = [
        "/opt/hermes/.venv/bin/python",
        str(EMAIL_HELPER),
        "--report",
        str(report_path),
        "--subject",
        subject,
    ]
    for recipient in recipients:
        base.extend(["--to", recipient])
    preview = subprocess.run(base, check=False, capture_output=True, text=True, timeout=60)
    if preview.returncode != 0:
        fail("email preview failed: " + preview.stderr.strip())
    token = next(
        (line.split("=", 1)[1] for line in preview.stdout.splitlines() if line.startswith("EMAIL_REPORT_CONFIRMATION_TOKEN=")),
        "",
    )
    if len(token) != 64:
        fail("email preview did not return a confirmation token")
    sent = subprocess.run(
        [*base, "--confirm-send", "--confirmation-token", token],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if sent.returncode != 0 or "EMAIL_REPORT_SENT=1" not in sent.stdout:
        fail("email delivery failed: " + (sent.stderr.strip() or sent.stdout.strip()))


def run(mode: str, *, force: bool = False) -> int:
    if mode not in {"model", "hf", "infra"}:
        fail("mode must be model, hf, or infra")
    now = datetime.now(tz=ZoneInfo("UTC"))
    is_due, local_date = due(mode, now)
    if not force and not is_due:
        return 0
    job_name = os.environ[
        {
            "model": "HERMES_REPORT_MODEL_JOB_NAME",
            "hf": "HERMES_REPORT_HF_JOB_NAME",
            "infra": "HERMES_REPORT_INFRA_JOB_NAME",
        }[mode]
    ]
    state_root = HERMES_HOME / "cron-state"
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    sent_marker = state_root / f"{job_name}-{local_date}.sent"
    saved_marker = state_root / f"{job_name}-{local_date}.saved"
    if (sent_marker.exists() or saved_marker.exists()) and not force:
        return 0
    recipients = [
        item.strip()
        for item in os.environ.get("HERMES_REPORT_RECIPIENTS", "").split(",")
        if item.strip()
    ]
    try:
        if mode == "model":
            subject, body = model_report(now)
        elif mode == "hf":
            subject, body = hugging_face_report(now)
        else:
            subject, body = infra_report(now)
    except Exception as exc:
        subject = f"🚨 SRE {mode} report: STATUS UNVERIFIED"
        body = f"# 🚨 Report collection failed\n\nNo current report is available. Collector error type: {type(exc).__name__}. Investigate the collector; do not treat this as a healthy result.\n"
    report_root = HERMES_HOME / "reports"
    report_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path = report_root / f"{job_name}-{local_date}.md"
    report_path.write_text(body, encoding="utf-8")
    report_path.chmod(0o600)
    if recipients:
        deliver(report_path, subject, recipients)
        marker = sent_marker
        outcome = "SENT"
    else:
        marker = saved_marker
        outcome = "SAVED"
    marker.write_text(now.isoformat() + "\n", encoding="utf-8")
    marker.chmod(0o600)
    print(f"SCHEDULED_REPORT_{outcome}={job_name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("model", "hf", "infra"), default=mode_from_program())
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    try:
        return run(arguments.mode, force=arguments.force)
    except Exception as exc:
        print(f"scheduled-report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
