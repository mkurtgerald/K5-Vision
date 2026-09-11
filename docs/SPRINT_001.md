# Sprint 001 — Platform Foundation

## Objective

Create a small, executable, testable control-plane foundation without prematurely locking K5 into a media implementation that has not been benchmarked.

## In scope

- Typed control-plane API
- Canonical initial device model
- In-memory device registry behind a service boundary
- Health endpoint
- Device create/list/get API
- CI lint/format/test gate
- 80% minimum coverage gate
- Architecture boundaries
- Commercial dependency policy
- Initial public-repo secret hygiene

## Acceptance criteria

- `pip install -e ".[dev]"` succeeds on Python 3.12
- `ruff check src tests` passes
- `ruff format --check src tests` passes
- `pytest` passes at >=80% branch-aware coverage
- `GET /api/v1/health` returns healthy status and version
- A camera can be registered with canonical protocols/tags and retrieved by ID
- CI runs for pull requests and pushes to main
- No sustained media-frame processing is implemented in Python

## Explicitly deferred

- ONVIF network discovery/authentication
- RTSP ingest
- Recording/playback
- Persistent database
- Authentication/authorization
- UI
- Computer vision
- Agentic automation

Those are not removed from the vision; they are sequenced after the platform contract and CI gate exist.

## Next vertical slice

Sprint 002 should prove one real-camera path:

1. Discover/probe an ONVIF camera
2. Normalize capabilities into K5's domain model
3. Acquire stream profile/URI metadata
4. Hand the URI to a benchmarkable media-worker boundary
5. Report connection/stream health back through the control plane
