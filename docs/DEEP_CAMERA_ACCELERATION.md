# DeepCamera Acceleration Import

This change selectively adapts useful architecture from SharpAI/DeepCamera without
turning DeepCamera into K5's VMS, control plane, recorder, identity plane, or evidence
system.

## Imported acceleration concepts

- JSONL detector process boundary with detector-neutral detection responses.
- Cross-platform accelerator discovery for NVIDIA, AMD, Apple Silicon, Intel, and CPU.
- Replaceable analytics runtimes behind a stable product-owned interface.
- Performance-oriented separation between media presentation and optional inference.

## K5 changes

K5 uses its existing `AnalyticsObservationProvider` seam. The new
`JsonlSkillProvider` supplies observations to that seam while keeping K5 authority
over source selection, credentials, authorization, media lifecycle, overlay rendering,
recording, audit, and evidence.

Unlike DeepCamera's frame-path protocol, K5 sends no decoded camera frame to disk.
Each requested frame is copied into OS shared memory, identified only by an ephemeral
handle, and unlinked immediately after the detector response.

The child receives:

- frame sequence number
- shared-memory handle
- byte length
- geometry/stride/pixel format
- source-relative timing

It does not receive:

- RTSP/ONVIF source URI
- credentials
- site/device identity
- user/operator identity
- recording/evidence paths
- retained media

## Deliberately excluded

- SharpAI Aegis application code.
- Legacy DeepCamera recorder/control-plane components.
- DeepCamera camera credential handling.
- Automatic LLM-driven package installation.
- Unpinned package installation.
- YOLO/Ultralytics model weights or runtime dependency.
- InsightFace weights.
- Depth Anything weights.
- Any model or artifact without separate commercial provenance clearance.

## Provenance boundary

The accelerator probing strategy is adapted from DeepCamera
`skills/lib/env_config.py` at commit
`933dcc7c90226cd1f92dc6ea7cee3b3c94790ad0` under MIT.

No DeepCamera model weights or separately licensed artifacts are incorporated by this
change. Detector packages and model artifacts remain independently replaceable and
must pass K5's commercial dependency policy before production distribution.
