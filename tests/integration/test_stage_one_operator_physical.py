"""Physical Stage-One witness for authenticated enrollment-to-Windows-live operation.

The witness deliberately retains only aggregate counters. Private source/credential
material is consumed in-process, and temporary durable user/device state is removed
from the camera-lab host after the test. If the composed operator launch fails, a
second transient diagnostic pass reports only sanitized boundary/error codes so the
physical gate can be repaired without exposing camera identity, credentials, paths,
or media.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from k5vision.media.live_presentation import BoundedLivePresentationDelivery, LivePresentationError
from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.rtp_delivery import EphemeralRtpDelivery, RtpDeliveryError
from k5vision.media.windows_presentation_surface import (
    BoundedWindowsPresentationSurface,
    WindowsPresentationSurfaceError,
)
from k5vision.media.windows_presentation_target import (
    BoundedWindowsPresentationTarget,
    WindowsPresentationTargetError,
)
from k5vision.stage03_credential_probe import resolve_credential_index
from k5vision.stage03_credentials import selected_source_uri
from k5vision.stage_one_app import create_stage_one_app

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE_ONE_OPERATOR_PHYSICAL") != "1",
    reason="Stage One authenticated physical operator qualification is opt-in",
)

_ADMIN_TOKEN = "stage-one-physical-bootstrap-admin"
_SITE_ID = "stage-one-physical-witness"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _remove_temporary_state(root: Path) -> None:
    for path in root.glob("stage-one-*.sqlite3*"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class _PhysicalAnalyticsProvider:
    """Exact reviewed Analytics-lab detector/tracker adapter for this witness only."""

    def __init__(self, evidence_root: Path) -> None:
        from analytics_lab.iou_tracker import SimpleIoUAssociationBackend
        from analytics_lab.openvino_omz import OpenVINOOMZConfig, OpenVINOOMZPoseBackend
        from analytics_lab.tracking import TrackingSession

        manifest_path = (evidence_root / "validation-manifest.json").resolve(strict=True)
        manifest_path.relative_to(evidence_root)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_root = (evidence_root / str(document.get("artifact_root", "artifacts"))).resolve(
            strict=True
        )
        artifact_root.relative_to(evidence_root)

        self._detector = OpenVINOOMZPoseBackend(
            artifact_root,
            config=OpenVINOOMZConfig(max_people=4),
        )
        self._tracker = TrackingSession(SimpleIoUAssociationBackend())
        self._frame_index = 0
        self.provider_calls = 0
        self.tracked_detections = 0

    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]:
        import numpy as np
        from analytics_lab.tracking import DetectionCandidate, NormalizedBox

        self.provider_calls += 1
        frame_index = self._frame_index
        self._frame_index += 1
        timestamp_ms = frame_index * 40 + 1

        raw = np.frombuffer(
            frame.payload,
            dtype=np.uint8,
            count=frame.height * frame.stride_bytes,
        )
        rows = raw.reshape(frame.height, frame.stride_bytes)
        bgrx = rows[:, : frame.width * 4].reshape(frame.height, frame.width, 4)
        bgr = np.ascontiguousarray(bgrx[:, :, :3])

        poses = await asyncio.to_thread(
            self._detector,
            bgr,
            frame_index,
            timestamp_ms,
        )
        candidates = []
        for pose in poses:
            box = pose.bbox
            x_min = min(1.0, max(0.0, float(box.x1) / frame.width))
            y_min = min(1.0, max(0.0, float(box.y1) / frame.height))
            x_max = min(1.0, max(0.0, float(box.x2) / frame.width))
            y_max = min(1.0, max(0.0, float(box.y2) / frame.height))
            if x_max <= x_min or y_max <= y_min:
                continue
            candidates.append(
                DetectionCandidate(
                    category="person",
                    confidence=pose.confidence,
                    box=NormalizedBox(x_min, y_min, x_max, y_max),
                    model_class_id=1,
                )
            )

        tracks = self._tracker.update(frame_index, timestamp_ms, tuple(candidates))
        self.tracked_detections += len(tracks)
        return tracks


async def _diagnose_private_windows_live(source: str, private_credentials: str) -> str:
    """Return one source-free failure token for the physical live path.

    This is intentionally a failure-only witness aid. It reuses the accepted transient
    Stage-35 delivery/surface path plus the real Win32 target, retains no media, and
    never includes source, credential, native-handle, path, or payload material in the
    returned token.
    """
    try:
        credential_index = await asyncio.to_thread(
            resolve_credential_index,
            source,
            private_credentials,
        )
    except Exception:
        return "source-resolution-failure"
    if credential_index is None:
        return "source-resolution-failure"

    try:
        authenticated_source = selected_source_uri(
            source,
            private_credentials,
            credential_index,
        )
    except Exception:
        return "source-resolution-failure"

    payload_type: int | None = None

    async def capture_payload_type(packet: memoryview) -> None:
        nonlocal payload_type
        candidate = int(packet[1] & 0x7F)
        if 96 <= candidate <= 127:
            payload_type = candidate

    probe_delivery = EphemeralRtpDelivery(
        packet_goal=1,
        delivery_timeout_seconds=15.0,
        consumer_timeout_seconds=2.0,
        relay_startup_probe_seconds=0.5,
    )
    try:
        await probe_delivery.deliver(authenticated_source, capture_payload_type)
    except RtpDeliveryError as exc:
        return f"rtp-probe:{exc.code.value}"
    except Exception:
        return "rtp-probe:unexpected"
    if payload_type is None:
        return "rtp-probe:unsupported-payload"

    surface = BoundedWindowsPresentationSurface(
        max_frames=100_000,
        max_frame_bytes=64 * 1024 * 1024,
        max_total_frame_bytes=8 * 1024 * 1024 * 1024,
        max_surface_replacements=4,
    )
    target = BoundedWindowsPresentationTarget(max_presentations=100_000)
    child_failure: str | None = None

    try:
        try:
            await surface.open()
        except WindowsPresentationSurfaceError as exc:
            return f"surface-open:{exc.code.value}"
        except Exception:
            return "surface-open:unexpected"

        try:
            await target.open(1280, 720)
        except WindowsPresentationTargetError as exc:
            return f"target-open:{exc.code.value}"
        except Exception:
            return "target-open:unexpected"

        async def present(frame: object) -> None:
            nonlocal child_failure
            try:
                await surface.present(frame)  # type: ignore[arg-type]
            except WindowsPresentationSurfaceError as exc:
                child_failure = f"surface-present:{exc.code.value}"
                raise
            except Exception:
                child_failure = "surface-present:unexpected"
                raise

            try:
                await target.present(surface)
            except WindowsPresentationTargetError as exc:
                child_failure = f"target-present:{exc.code.value}"
                raise
            except Exception:
                child_failure = "target-present:unexpected"
                raise

        delivery = BoundedLivePresentationDelivery(
            payload_type,
            packet_goal=2048,
            delivery_timeout_seconds=30.0,
            packet_consumer_timeout_seconds=4.0,
            decoder_timeout_seconds=2.0,
            frame_consumer_timeout_seconds=2.0,
            cleanup_timeout_seconds=2.0,
            relay_startup_probe_seconds=0.5,
        )
        try:
            snapshot = await delivery.run(authenticated_source, present)  # type: ignore[arg-type]
        except LivePresentationError as exc:
            return child_failure or f"live-delivery:{exc.code.value}"
        except Exception:
            return child_failure or "live-delivery:unexpected"

        if snapshot.delivered_frames < 1 or target.snapshot.presentations < 1:
            return "direct-live:no-presentations"
        return "direct-live:ok"
    finally:
        try:
            await target.close()
        except Exception:
            pass
        try:
            await surface.close()
        except Exception:
            pass


def test_authenticated_enrollment_launches_private_source_in_windows_operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = os.environ["K5_STAGE03_SOURCE"]
    private_credentials = os.environ["K5_STAGE03_CAM_CRED"]
    output = Path(os.environ["K5_STAGE_ONE_OUTPUT"])
    revision = os.environ["K5_STAGE_ONE_REVISION"].casefold()
    analytics_root = Path(os.environ["K5_ANALYTICS_EVIDENCE_ROOT"]).resolve(strict=True)

    parsed = urlsplit(source)
    host = parsed.hostname
    assert host is not None

    device_db = tmp_path / "stage-one-devices.sqlite3"
    user_db = tmp_path / "stage-one-users.sqlite3"
    monkeypatch.setenv("K5_CONTROL_PLANE_SITE_ID", _SITE_ID)
    monkeypatch.setenv("K5_DEVICE_DB_PATH", str(device_db))
    monkeypatch.setenv("K5_USER_DB_PATH", str(user_db))
    monkeypatch.setenv("K5_CONTROL_PLANE_ADMIN_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("K5_OPERATOR_STREAM_TOKEN", "main")
    monkeypatch.delenv("K5_OPERATOR_RTP_PAYLOAD_TYPE", raising=False)

    administrator_username = f"physical-administrator-{secrets.token_hex(6)}"
    administrator_password = secrets.token_urlsafe(32)
    administrator_temporary_credential = ""
    administrator_session_token = ""
    operator_username = f"physical-operator-{secrets.token_hex(6)}"
    operator_password = secrets.token_urlsafe(32)
    operator_temporary_credential = ""
    operator_session_token = ""
    analytics_provider = _PhysicalAnalyticsProvider(analytics_root)

    try:
        application = create_stage_one_app(detection_provider=analytics_provider)
        with TestClient(application) as client:
            administrator_created = client.post(
                "/api/v1/users",
                json={
                    "username": administrator_username,
                    "display_name": "Stage One Physical Administrator",
                    "role": "administrator",
                    "enabled": True,
                },
                headers=_headers(_ADMIN_TOKEN),
            )
            assert administrator_created.status_code == 201
            administrator_temporary_credential = administrator_created.json()[
                "temporary_credential"
            ]

            administrator_initialized = client.post(
                "/api/v1/auth/bootstrap-password",
                json={
                    "username": administrator_username,
                    "temporary_credential": administrator_temporary_credential,
                    "new_password": administrator_password,
                },
            )
            assert administrator_initialized.status_code == 204

            administrator_logged_in = client.post(
                "/api/v1/auth/login",
                json={
                    "username": administrator_username,
                    "password": administrator_password,
                },
            )
            assert administrator_logged_in.status_code == 200
            administrator_session_token = administrator_logged_in.json()["session_token"]

            operator_created = client.post(
                "/api/v1/users",
                json={
                    "username": operator_username,
                    "display_name": "Stage One Physical Operator",
                    "role": "operator",
                    "enabled": True,
                },
                headers=_headers(_ADMIN_TOKEN),
            )
            assert operator_created.status_code == 201
            operator_temporary_credential = operator_created.json()["temporary_credential"]

            operator_initialized = client.post(
                "/api/v1/auth/bootstrap-password",
                json={
                    "username": operator_username,
                    "temporary_credential": operator_temporary_credential,
                    "new_password": operator_password,
                },
            )
            assert operator_initialized.status_code == 204

            operator_logged_in = client.post(
                "/api/v1/auth/login",
                json={"username": operator_username, "password": operator_password},
            )
            assert operator_logged_in.status_code == 200
            operator_session_token = operator_logged_in.json()["session_token"]

            enrolled = client.post(
                "/api/v1/devices",
                json={
                    "name": "Stage One Physical Camera",
                    "host": host,
                    "kind": "camera",
                    "protocols": ["rtsp"],
                    "tags": ["stage-one-physical-witness"],
                },
                headers=_headers(administrator_session_token),
            )
            assert enrolled.status_code in {200, 201}
            device_id = enrolled.json()["id"]

            service_token_attempt = client.post(
                "/api/v1/operator/live",
                json={"device_id": device_id, "stream_token": "main"},
                headers=_headers(_ADMIN_TOKEN),
            )
            assert service_token_attempt.status_code == 401

            launched = client.post(
                "/api/v1/operator/live",
                json={
                    "device_id": device_id,
                    "stream_token": "main",
                    "width": 1280,
                    "height": 720,
                },
                headers=_headers(operator_session_token),
            )
            if launched.status_code != 200:
                diagnostic = asyncio.run(
                    _diagnose_private_windows_live(source, private_credentials)
                )
                pytest.fail(
                    f"Stage One live operator runtime failed; source-free diagnostic={diagnostic}"
                )

            receipt = launched.json()
            assert receipt["completed"] is True
            assert receipt["delivered_frames"] >= 1
            assert receipt["presentations"] >= 1
            assert receipt["analytics_enabled"] is True
            assert receipt["analytics_provider_submissions"] >= 1
            assert receipt["analytics_provider_completions"] >= 1
            assert receipt["analytics_failures"] == 0
            assert receipt["analytics_rendered_boxes"] >= 1

        evidence = {
            "schema_version": "3",
            "revision": revision,
            "execution_context": "camera-lab-windows-x64",
            "human_session_authenticated": True,
            "device_enrolled": True,
            "service_token_rejected": True,
            "windows_live_launch_completed": True,
            "delivered_frames": receipt["delivered_frames"],
            "presentations": receipt["presentations"],
            "processed_controls": receipt["processed_controls"],
            "analytics_provider_calls": analytics_provider.provider_calls,
            "analytics_tracked_detections": analytics_provider.tracked_detections,
            "analytics_provider_submissions": receipt["analytics_provider_submissions"],
            "analytics_provider_completions": receipt["analytics_provider_completions"],
            "analytics_failures": receipt["analytics_failures"],
            "analytics_rendered_boxes": receipt["analytics_rendered_boxes"],
        }
        payload = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        lowered = payload.casefold()
        for forbidden in (
            source,
            host,
            private_credentials,
            administrator_username,
            administrator_temporary_credential,
            administrator_password,
            administrator_session_token,
            operator_username,
            operator_temporary_credential,
            operator_password,
            operator_session_token,
            _ADMIN_TOKEN,
            str(analytics_root),
        ):
            assert forbidden and forbidden not in payload
        for forbidden_marker in (
            "rtsp://",
            "rtsps://",
            "credential",
            "password",
            "session_token",
            "device_id",
            "source_uri",
            "runner_name",
            "payload",
        ):
            assert forbidden_marker not in lowered

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    finally:
        _remove_temporary_state(tmp_path)
