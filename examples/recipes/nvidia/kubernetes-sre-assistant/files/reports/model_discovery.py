#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Publish a daily Hugging Face model report and email its Hermes review links.

The script intentionally uses only the Python standard library so the chart can
reuse its existing digest-pinned Python image. It persists the report before
sending email, which prevents a message from containing links to data Hermes
cannot yet review.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


HUGGING_FACE_MODELS_API = "https://huggingface.co/api/models"
HUGGING_FACE_SOURCE_URL = (
    "https://huggingface.co/models?num_parameters=min%3A12B&apps=vllm"
    "&base_model_relation=base&sort=trending"
)
MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
REPORT_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
NVIDIA_SENDER_RE = re.compile(
    r"^[^@\s\r\n]+@(?:nvidia|exchange\.nvidia)\.com$", re.IGNORECASE
)
EMAIL_ADDRESS_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
MAX_EMAIL_RECIPIENTS = 25
MIN_PARAMETERS = 12_000_000_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SERVICE_ACCOUNT_ROOT = Path("/var/run/secrets/kubernetes.io/serviceaccount")

EXPANDED_FIELDS = (
    "createdAt",
    "lastModified",
    "downloads",
    "likes",
    "trendingScore",
    "pipeline_tag",
    "library_name",
    "gated",
    "safetensors",
)


def _required(env: dict[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if not value:
        raise ValueError(f"{name} is required")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains a prohibited newline")
    return value


def _optional(env: dict[str, str], name: str) -> str:
    value = str(env.get(name, "")).strip()
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains a prohibited newline")
    return value


def _integer(env: dict[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(env.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_smtp_port(env: dict[str, str]) -> int | None:
    raw = str(env.get("MODEL_DISCOVERY_SMTP_PORT", "")).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("model discovery email configuration is partial") from exc
    if value < 1 or value > 65535:
        raise ValueError("model discovery email configuration is partial")
    return value


def _normalize_recipients(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = [] if not value.strip() else [value.strip()]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        raise ValueError("email recipient list must be a string or array")
    if len(candidates) > MAX_EMAIL_RECIPIENTS:
        raise ValueError(f"email recipient list must contain at most {MAX_EMAIL_RECIPIENTS} entries")
    recipients: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ValueError("email recipient must be a string")
        recipient = candidate.strip()
        if not recipient or "\r" in recipient or "\n" in recipient or not EMAIL_ADDRESS_RE.fullmatch(recipient):
            raise ValueError("email recipient is malformed")
        normalized = recipient.casefold()
        if normalized in seen:
            raise ValueError("email recipient list contains a duplicate recipient")
        seen.add(normalized)
        recipients.append(recipient)
    return tuple(recipients)


def _recipients_from_env(env: dict[str, str]) -> tuple[str, ...]:
    legacy = str(env.get("MODEL_DISCOVERY_EMAIL_TO", "")).strip()
    encoded = str(env.get("MODEL_DISCOVERY_EMAIL_TO_JSON", "")).strip()
    if legacy and encoded:
        raise ValueError("email recipient configuration is ambiguous")
    if encoded:
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError("email recipient JSON is malformed") from exc
        return _normalize_recipients(decoded)
    return _normalize_recipients(legacy)


def _validate_base_url(value: str) -> str:
    # Empty means "no review links": the headless profile (webui.enabled=false)
    # still emails the report but has no WebUI approval surface to link to.
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MODEL_DISCOVERY_WEBUI_BASE_URL must be an HTTPS URL without user info")
    if parsed.query or parsed.fragment:
        raise ValueError("MODEL_DISCOVERY_WEBUI_BASE_URL must not contain a query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class DiscoveryConfig:
    namespace: str
    report_configmap: str
    webui_base_url: str
    smtp_host: str
    smtp_port: int | None
    email_from: str
    email_to: str | tuple[str, ...]
    top_n: int = 10
    request_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if len(self.namespace) > 63 or not DNS_LABEL_RE.fullmatch(self.namespace):
            raise ValueError("namespace must be a DNS label")
        if len(self.report_configmap) > 253 or not DNS_LABEL_RE.fullmatch(self.report_configmap):
            raise ValueError("report_configmap must be a DNS label")
        object.__setattr__(self, "webui_base_url", _validate_base_url(self.webui_base_url))
        smtp_host = str(self.smtp_host).strip()
        email_from = str(self.email_from).strip()
        recipients = _normalize_recipients(self.email_to)
        object.__setattr__(self, "smtp_host", smtp_host)
        object.__setattr__(self, "email_from", email_from)
        object.__setattr__(self, "email_to", recipients)
        if not email_from and not recipients:
            pass
        elif (
            not smtp_host
            or any(character in smtp_host for character in "\r\n")
            or self.smtp_port is None
            or self.smtp_port < 1
            or self.smtp_port > 65535
            or not email_from
            or not recipients
        ):
            raise ValueError("model discovery email configuration is partial")
        elif not NVIDIA_SENDER_RE.fullmatch(email_from):
            raise ValueError("email_from must be an NVIDIA email address")
        if self.top_n < 1 or self.top_n > 25:
            raise ValueError("top_n must be between 1 and 25")
        if self.request_timeout_seconds < 1 or self.request_timeout_seconds > 120:
            raise ValueError("request_timeout_seconds must be between 1 and 120")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "DiscoveryConfig":
        values = dict(os.environ if environ is None else environ)
        return cls(
            namespace=_required(values, "POD_NAMESPACE"),
            report_configmap=_required(values, "MODEL_DISCOVERY_REPORT_CONFIGMAP"),
            webui_base_url=_optional(values, "MODEL_DISCOVERY_WEBUI_BASE_URL"),
            smtp_host=_optional(values, "MODEL_DISCOVERY_SMTP_HOST"),
            smtp_port=_optional_smtp_port(values),
            email_from=_optional(values, "MODEL_DISCOVERY_EMAIL_FROM"),
            email_to=_recipients_from_env(values),
            top_n=_integer(values, "MODEL_DISCOVERY_TOP_N", 10, 1, 25),
            request_timeout_seconds=_integer(
                values, "MODEL_DISCOVERY_REQUEST_TIMEOUT_SECONDS", 30, 1, 120
            ),
        )

    @property
    def email_recipients(self) -> tuple[str, ...]:
        return tuple(self.email_to)

    @property
    def email_enabled(self) -> bool:
        return bool(self.email_from and self.email_recipients)


def build_hugging_face_url(limit: int = 10) -> str:
    if limit < 1 or limit > 25:
        raise ValueError("limit must be between 1 and 25")
    parameters: list[tuple[str, str]] = [
        ("sort", "trendingScore"),
        ("limit", str(limit)),
        ("num_parameters", "min:12B"),
        ("apps", "vllm"),
        ("base_model_relation", "base"),
    ]
    parameters.extend(("expand", field) for field in EXPANDED_FIELDS)
    return f"{HUGGING_FACE_MODELS_API}?{urlencode(parameters)}"


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Hugging Face returned an invalid {field}")
    return value


def _optional_text(value: object, field: str, maximum: int = 128) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"Hugging Face returned an invalid {field}")
    return value


def _model_url(model_id: str) -> str:
    owner, name = model_id.split("/", 1)
    return f"https://huggingface.co/{quote(owner, safe='')}/{quote(name, safe='')}"


def normalize_models(payload: object, limit: int = 10) -> list[dict[str, object]]:
    if not isinstance(payload, list) or len(payload) < limit:
        raise ValueError(f"Hugging Face returned fewer than {limit} models")

    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for rank, raw in enumerate(payload[:limit], start=1):
        if not isinstance(raw, dict):
            raise ValueError("Hugging Face returned an invalid model entry")
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id):
            raise ValueError("Hugging Face returned an invalid model id")
        if model_id in seen:
            raise ValueError("Hugging Face returned a duplicate model id")
        seen.add(model_id)

        safetensors = raw.get("safetensors")
        if not isinstance(safetensors, dict):
            raise ValueError(f"Hugging Face did not return parameter metadata for {model_id}")
        parameters = _nonnegative_integer(safetensors.get("total"), "parameter count")
        if parameters < MIN_PARAMETERS:
            raise ValueError(f"Hugging Face returned a model below 12B parameters: {model_id}")

        gated = raw.get("gated")
        if gated is None:
            gated_value: bool | str = False
        elif isinstance(gated, bool):
            gated_value = gated
        elif isinstance(gated, str) and gated in {"auto", "manual"}:
            gated_value = gated
        else:
            raise ValueError(f"Hugging Face returned invalid gated metadata for {model_id}")

        normalized.append(
            {
                "rank": rank,
                "id": model_id,
                "url": _model_url(model_id),
                "parameters": parameters,
                "trendingScore": _nonnegative_integer(raw.get("trendingScore"), "trending score"),
                "downloads": _nonnegative_integer(raw.get("downloads"), "download count"),
                "likes": _nonnegative_integer(raw.get("likes"), "like count"),
                "createdAt": _optional_text(raw.get("createdAt"), "creation timestamp", 64),
                "lastModified": _optional_text(raw.get("lastModified"), "modified timestamp", 64),
                "pipelineTag": _optional_text(raw.get("pipeline_tag"), "pipeline tag", 96),
                "libraryName": _optional_text(raw.get("library_name"), "library name", 96),
                "gated": gated_value,
            }
        )
    return normalized


def fetch_models(limit: int, timeout_seconds: int) -> object:
    request = Request(
        build_hugging_face_url(limit),
        headers={"Accept": "application/json", "User-Agent": "hermes-model-discovery/1.0"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("Hugging Face response exceeded the size limit")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Hugging Face returned invalid JSON") from exc


def build_report(models: list[dict[str, object]], *, now: datetime | None = None) -> dict[str, object]:
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ValueError("report time must be timezone-aware")
    generated = generated.astimezone(timezone.utc).replace(microsecond=0)
    generated_at = generated.isoformat().replace("+00:00", "Z")
    identifiers = "\n".join(str(model["id"]) for model in models)
    suffix = hashlib.sha256(f"{generated_at}\n{identifiers}".encode("utf-8")).hexdigest()[:8]
    report_id = f"{generated.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
    return {
        "schemaVersion": 1,
        "reportId": report_id,
        "generatedAt": generated_at,
        "sourceUrl": HUGGING_FACE_SOURCE_URL,
        "query": {
            "sort": "trending",
            "apiSort": "trendingScore",
            "minimumParameters": MIN_PARAMETERS,
            "application": "vllm",
            "baseModelRelation": "base",
            "limit": len(models),
        },
        "models": models,
    }


def approval_url(webui_base_url: str, report_id: str, model_id: str) -> str:
    base = _validate_base_url(webui_base_url)
    if not REPORT_ID_RE.fullmatch(report_id):
        raise ValueError("report id is invalid")
    if not MODEL_ID_RE.fullmatch(model_id):
        raise ValueError("model id is invalid")
    if not base:
        return ""
    parsed = urlsplit(base)
    path = (parsed.path.rstrip("/") + "/") if parsed.path else "/"
    query = urlencode({"model-discovery": report_id, "model": model_id})
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def _parameter_label(value: object) -> str:
    parameters = int(value)
    return f"{parameters / 1_000_000_000:.1f}B"


def build_email(
    report: dict[str, object],
    *,
    sender: str,
    recipient: str | tuple[str, ...],
    webui_base_url: str,
) -> EmailMessage:
    if not NVIDIA_SENDER_RE.fullmatch(sender):
        raise ValueError("sender must be an NVIDIA email address")
    recipients = _normalize_recipients(recipient)
    if not recipients:
        raise ValueError("email recipient list is empty")
    models = report.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("report contains no models")
    report_id = str(report.get("reportId", ""))
    generated_at = str(report.get("generatedAt", ""))

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"Hermes daily Hugging Face model report - {generated_at[:10]}"
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain="nvidia.com")
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message["Precedence"] = "bulk"

    plain_lines = [
        "Hermes daily Hugging Face model discovery report",
        f"Generated: {generated_at}",
        "Filters: at least 12B parameters, vLLM, base models, sorted by trending score.",
        "",
    ]
    html_rows: list[str] = []
    for model in models:
        model_id = str(model["id"])
        review_url = approval_url(webui_base_url, report_id, model_id)
        plain_lines.extend(
            [
                f'{model["rank"]}. {model_id} ({_parameter_label(model["parameters"])})',
                f'   Trending score: {model["trendingScore"]}; downloads: {model["downloads"]}; likes: {model["likes"]}',
                f'   Released: {model["createdAt"] or "unknown"}; updated: {model["lastModified"] or "unknown"}',
                f'   Model: {model["url"]}',
            ]
        )
        if review_url:
            plain_lines.append(f"   Review and approve in Hermes: {review_url}")
        plain_lines.append("")
        review_cell = (
            f'<a href="{html.escape(review_url, quote=True)}">Review &amp; approve</a>'
            if review_url
            else "&mdash;"
        )
        html_rows.append(
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db">{int(model["rank"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db"><a href="{html.escape(str(model["url"]), quote=True)}">{html.escape(model_id)}</a></td>'
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db">{html.escape(_parameter_label(model["parameters"]))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db">{int(model["trendingScore"])}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db">{html.escape(str(model["createdAt"] or "unknown"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #d1d5db">{review_cell}</td>'
            "</tr>"
        )
    if webui_base_url:
        plain_lines.extend(
            [
                "Opening a link never deploys a model. Deployment starts only after you sign in",
                "to Hermes and press the approval button; Hermes then runs its existing guarded",
                "openshift-llm-deploy workflow and asks for final confirmation before apply.",
                "",
            ]
        )
    else:
        plain_lines.extend(
            [
                "This deployment has no WebUI approval surface. Ask Hermes through its API or",
                "terminal to run the guarded openshift-llm-deploy workflow for a listed model.",
                "",
            ]
        )
    plain_lines.append(f"Source: {HUGGING_FACE_SOURCE_URL}")
    message.set_content("\n".join(plain_lines))

    html_body = (
        "<!doctype html><html><body style=\"font-family:Arial,sans-serif;color:#111827\">"
        "<h2>Hermes daily Hugging Face model discovery report</h2>"
        f"<p>Generated {html.escape(generated_at)}. Filters: at least 12B parameters, vLLM, base models, sorted by trending score.</p>"
        '<table style="border-collapse:collapse;width:100%"><thead><tr>'
        "<th align=\"left\">Rank</th><th align=\"left\">Model</th><th align=\"left\">Parameters</th>"
        "<th align=\"left\">Trending</th><th align=\"left\">Released</th><th align=\"left\">Hermes</th>"
        "</tr></thead><tbody>"
        + "".join(html_rows)
        + "</tbody></table>"
        "<p><strong>Safety:</strong> opening a link never deploys anything. After sign-in, a deliberate approval button starts Hermes' existing guarded deployment workflow, including final confirmation before apply.</p>"
        f'<p><a href="{html.escape(HUGGING_FACE_SOURCE_URL, quote=True)}">View the source query on Hugging Face</a></p>'
        "</body></html>"
    )
    message.add_alternative(html_body, subtype="html")
    return message


def _service_account_value(filename: str) -> str:
    path = SERVICE_ACCOUNT_ROOT / filename
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Kubernetes service account {filename} is empty")
    return value


def _configmap_url(config: DiscoveryConfig) -> str:
    # Address the API server by its in-cluster DNS name, never by the ClusterIP
    # that KUBERNETES_SERVICE_HOST carries. Hermes reaches the API through the
    # OpenShell policy proxy, whose kubernetes_api endpoint allows the service
    # name and denies the IP literal, so the native report backend fails at the
    # ConfigMap write with "Tunnel connection failed: 403 Forbidden" when the IP
    # is used. The service name also resolves inside the CronJob pod.
    host = os.environ.get("MODEL_DISCOVERY_KUBERNETES_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    host_literal = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return (
        f"https://{host_literal}:{port}/api/v1/namespaces/{quote(config.namespace, safe='')}"
        f"/configmaps/{quote(config.report_configmap, safe='')}"
    )


def _kubernetes_json_request(
    url: str,
    token: str,
    context: ssl.SSLContext,
    timeout_seconds: int,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hermes-model-discovery/1.0",
        },
    )
    with urlopen(request, context=context, timeout=timeout_seconds) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Kubernetes API response exceeded the size limit")
    result = json.loads(content.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Kubernetes API returned an invalid object")
    return result


def persist_report(report: dict[str, object], config: DiscoveryConfig) -> None:
    token = _service_account_value("token")
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT_ROOT / "ca.crt"))
    url = _configmap_url(config)
    report_json = json.dumps(report, separators=(",", ":"), sort_keys=True)
    if len(report_json.encode("utf-8")) > 512 * 1024:
        raise ValueError("report exceeds the configured size limit")

    for attempt in range(2):
        current = _kubernetes_json_request(
            url, token, context, config.request_timeout_seconds
        )
        metadata = current.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("resourceVersion"):
            raise RuntimeError("report ConfigMap has no resourceVersion")
        updated_metadata: dict[str, object] = {
            "name": config.report_configmap,
            "namespace": config.namespace,
            "resourceVersion": metadata["resourceVersion"],
        }
        for key in ("labels", "annotations", "ownerReferences", "finalizers"):
            if key in metadata:
                updated_metadata[key] = metadata[key]
        existing_data = current.get("data")
        data = dict(existing_data) if isinstance(existing_data, dict) else {}
        data["report.json"] = report_json
        replacement: dict[str, object] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": updated_metadata,
            "data": data,
        }
        try:
            _kubernetes_json_request(
                url,
                token,
                context,
                config.request_timeout_seconds,
                method="PUT",
                payload=replacement,
            )
            return
        except HTTPError as exc:
            if exc.code != 409 or attempt:
                raise
    raise RuntimeError("could not update report ConfigMap")


def send_report_email(message: EmailMessage, config: DiscoveryConfig) -> None:
    if not config.email_enabled or config.smtp_port is None:
        raise ValueError("model discovery email configuration is partial")
    with smtplib.SMTP(
        config.smtp_host,
        config.smtp_port,
        timeout=config.request_timeout_seconds,
    ) as smtp:
        smtp.send_message(message, to_addrs=config.email_recipients)


def run(
    config: DiscoveryConfig,
    *,
    fetcher: Callable[[int, int], object] = fetch_models,
    persister: Callable[[dict[str, object], DiscoveryConfig], None] = persist_report,
    mailer: Callable[[EmailMessage, DiscoveryConfig], None] = send_report_email,
    now: datetime | None = None,
) -> dict[str, object]:
    payload = fetcher(config.top_n, config.request_timeout_seconds)
    models = normalize_models(payload, limit=config.top_n)
    report = build_report(models, now=now)
    persister(report, config)
    if config.email_enabled:
        message = build_email(
            report,
            sender=config.email_from,
            recipient=config.email_recipients,
            webui_base_url=config.webui_base_url,
        )
        mailer(message, config)
    return report


def main() -> None:
    try:
        config = DiscoveryConfig.from_env()
        report = run(config)
    except Exception as exc:
        raise SystemExit(f"model-discovery: {type(exc).__name__}: {exc}") from exc
    delivery = "sent configured email" if config.email_enabled else "email delivery disabled"
    print(
        f'model-discovery: published {len(report["models"])} models in report '
        f'{report["reportId"]}; {delivery}',
        flush=True,
    )


if __name__ == "__main__":
    main()
