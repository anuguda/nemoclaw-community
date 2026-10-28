<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenClaw validation — 2026-10-27 and 2026-10-28

The existing deployer and SRE umbrella chart were installed on OpenShift with
`deployer.agent.runtime=openclaw`, using the official NemoClaw v0.0.130 image:

```text
ghcr.io/nvidia/nemoclaw/openclaw-sandbox@sha256:0cc9a96c065f02bf0c483bc74bd23f9b3ffec538f49557c77b1dafd73d9e0480
```

No custom image build, registry push, or new controller was used. The default
runtime remains NemoClaw-managed Hermes. OpenClaw was selected with the existing
[OpenClaw overlay](../values-openclaw.example.yaml) and private site values;
credentials and site endpoints are not included in this report.

## Observed results — 2026-10-27

| Check | Result |
| --- | --- |
| Helm installation and upgrade | Deployed successfully in an isolated namespace, with Slack, reports, and autoheal disabled for that release |
| OpenShell gateway state | Sandbox reported `Ready` |
| Gateway-mediated runtime execution | `openclaw --version` reported `OpenClaw 2026.9.2 (3928bad)` |
| Real model/tool execution | Agent trace recorded two successful native `exec` calls, no tool failures, and no model fallback |
| SRE resource access | Listed the isolated namespace's SRE proxy Deployment |
| Credential boundary | Listing Kubernetes Secrets returned `Forbidden` |
| Workspace retention | A harmless workspace marker survived acknowledged chart-driven sandbox deletion and recreation; marker removed afterward |
| Existing native Slack bot | Received a human mention, replied in the originating thread with the requested read-only results and `good to go` |
| Automated checks | 117 deployer tests and 221 SRE tests passed; four SRE tests cover runtime-specific install instructions |

The recreated sandbox used a different Pod UID and the retained workspace PVC.
The test establishes workspace retention, not persistence of every native session
or gateway configuration file.

## Scope and limitations — 2026-10-27

- The isolated release validates the current working chart. The Slack exchange
  used the existing bot release; it was not upgraded or duplicated for this test.
- The existing Slack sandbox replied successfully while its OpenShell control
  plane reported `Error`. Its pod was Running, but these observations do not
  establish healthy gateway lifecycle management. That pre-existing inconsistency
  was left unchanged rather than resetting the active bot during this validation.
- The bot's configured streaming mode was `progress`, with native transport
  disabled. Message delivery passed; chunk timing and streaming performance were
  not benchmarked.
- OpenClaw requires `deployer.access.enabled=false`. The Hermes dashboard/API
  relay is not exposed for OpenClaw. Use the native terminal or optional Slack
  integration.
- Slack preparation includes the version- and checksum-checked SDK compatibility
  edit described in [Slack onboarding](slack-openclaw.md). The image is official,
  but that runtime dependency edit remains a qualification requirement for image
  upgrades. This is not a claim that every newer upstream image works unchanged.
- Native Kubernetes and public-cloud clusters were not deployed in this run.
- Memory-driven chief of staff remains a subsequent Hermes recipe integration;
  this validation covers optional OpenClaw in the existing SRE deployment.

Install notes now omit disabled dashboard/API/terminal resources, retain the
configured Hermes ports when enabled, and omit upgrade seed-Job commands when
no seed Job ran.

## Storage update verification — 2026-10-28

The optional `global.sre.storageDiagnostics.enabled` feature was enabled in an
isolated read-only OpenClaw release, then in an existing authorized Slack release.
The managed image above was retained. Environment names, resource identifiers,
credentials and private endpoints are omitted.

| Check | Observed result |
| --- | --- |
| Native storage collection | A real CSI-backed PVC was Bound; its PV claim-reference UID matched; Ceph reported `HEALTH_OK` |
| Missing resource | An absent PVC returned exit code 2 and `unverified`, not a healthy empty result |
| Read-only boundary | The validation proxy denied PVC expansion and Secret reads; fixture capacity stayed unchanged |
| Managed model/tool execution | The updated operational release completed four native `read`/`exec` tool calls with zero failures and no model fallback |
| Interpretation | The model returned the observed binding evidence and explicitly stated that filesystem fullness was not measured |
| Native Slack health | Account was connected/running; authentication probe returned HTTP 200; existing allowlists were retained |
| State-preserving upgrade | Workspace marker survived recreation; original agent PVC identities and protected credential data were unchanged; a verified native backup was copied off-pod before teardown |
| Report configuration | Existing infrastructure and model-discovery schedules remained enabled |
| Lifecycle defaults | One-time upgrade re-seeding was disabled after recreation |
| Cleanup | Isolated release, namespace, four PVC/PVs and four cluster RBAC objects were verified absent |

No Slack test message was posted during this update; its Slack result is a
connection/authentication check, not a new message-delivery round trip. Native
session/configuration state was backed up; the retention check establishes
workspace persistence, not migration of every native session.

Hermes storage-enabled Helm rendering, shared installation and read-only skill
mounts were also verified. Live Hermes storage/model execution was not exercised.
External storage telemetry clients and private skill content are not part of
this feature. The namespace-scoped helper reports Kubernetes storage metadata,
not application I/O, free space or filesystem-repair success.

## Default-runtime and merged security review — 2026-10-28

The SRE recipe now declares NemoClaw-managed Hermes explicitly in its own
values. The deployer and Slack example already selected Hermes. OpenClaw remains
an explicit overlay choice; scheduled reports reject Hermes rather than
silently selecting OpenClaw. No existing cluster release was changed in this
review.

PR [#201](https://github.com/NVIDIA/nemoclaw-community/pull/201), revision
`63a82dcd28b23c34df9ce0a0e89601693099e5c6`, was integrated while preserving the
optional runtime, Slack and storage-diagnostic changes:

- HF checks accept only the canonical configured Secret reference. The trusted
  proxy negotiates metadata, never reads the upstream body, constructs the
  response itself and refuses redirects. The same boundary applies to the
  optional protected-advanced policy; its existing mutation restrictions remain.
- Automatic storage expansion durably reserves the global action budget before
  submitting a PVC patch. An uncertain write retains its reservation; corrupt
  memory fails closed. Automatic storage requires a single writer.
- Public models can download anonymously. Authentication failures provide
  operator-managed Secret guidance without printing vendor exception contents.

The source review covered proxy authorization, credential mediation, Slack
allowlists, runtime preparation, reports, storage writes and deployment helpers.
A targeted credential/private-identifier audit covered 103 changed source files
and the decoded skill archive. No additional exploitable issue was confirmed
within this scope. Automated security-scanner and independent-review invocations
were not authorized, so this is an inline source review, not a scanner result or
independent security certification.

Local verification:

| Check | Result |
| --- | --- |
| Runtime Helm rendering | Default Hermes, Hermes with storage, Hermes with Slack, optional OpenClaw, and OpenClaw with Slack reports passed |
| Fail-closed configuration | Mismatched runtime/image, Hermes scheduled reports and empty Slack allowlists rejected |
| Native preparation | Both runtimes disabled stale Slack configuration, preserved selected model configuration and applied private file permissions in temporary homes |
| HTTP proxy exercise | Both policies returned constructed Secret metadata and denied query/watch variants, other Secrets, RBAC writes and forbidden deletion without forwarding |
| SRE suite | 263 tests passed, including storage-budget crash/restart and Secret redirect/body-isolation coverage |
| Deployer suite | 117 tests passed |
| Helm lint | SRE recipe, deployer and autoheal charts passed |
| Community checks | 1,023 source-file SPDX checks, 63-label taxonomy and whitespace checks passed |
| Syntax checks | 58 changed Python sources and two changed shell scripts passed |

The merged optional-HF tests initially leaked the preflight sandbox PATH into
later Helm checks. Their environment is now isolated; the complete SRE suite
passes without changing OpenShell database behavior. Fixture HTTP-error resource
warnings remain non-fatal.

This verification did not start a live Hermes gateway, redeploy a cluster,
deliver a new Slack message or exercise live PVC expansion. Helm values remain
trusted operator input. Mutation-enabled access modes grant substantial workload
authority; direct Secret/exec denial is not a substitute for namespace isolation,
admission policy or a full authorization broker. Slack report recipients must be
authorized to receive the collected cluster information.
