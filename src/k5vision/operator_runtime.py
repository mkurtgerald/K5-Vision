"""Private Stage-One bridge from enrolled selection to accepted Windows live video.

The public control plane selects only a device UUID and opaque stream token. This
module keeps the physical RTSP source and credential bundle inside a private runtime
boundary, then composes the already-qualified ephemeral live-presentation and Windows
operator runtimes. Retained results contain counters only; source URIs, credentials,
media payloads, and native window identities never cross this boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol
from urllib.parse import urlsplit

from k5vision.domain.devices import Device
from k5vision.media.analytics_overlay_delivery import (
    AnalyticsObservationProvider,
    BoundedAnalyticsOverlayDelivery,
)
from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_runtime import BoundedPresentationRuntime
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import (
    OperatorLauncher,
    OperatorLaunchError,
    OperatorLaunchErrorCode,
    OperatorLaunchMetrics,
    OperatorSourceResolver,
    ResolvedLiveSource,
)
from k5vision.stage03_credential_probe import resolve_credential_index
from k5vision.stage03_credentials import selected_source_uri

STAGE_ONE_SOURCE_ENV = "K5_STAGE03_SOURCE"
STAGE_ONE_CREDENTIAL_ENV = "K5_STAGE03_CAM_CRED"
STAGE_ONE_STREAM_TOKEN_ENV = "K5_OPERATOR_STREAM_TOKEN"
STAGE_ONE_PAYLOAD_TYPE_ENV = "K5_OPERATOR_RTP_PAYLOAD_TYPE"
_DEFAULT_STREAM_TOKEN = "main"
_DEFAULT_PAYLOAD_TYPE = 96
_MAX_STREAM_TOKEN_LENGTH = 256


CredentialProbe = Callable[[str, str], int | None]
PayloadTypeProbe = Callable[[str], Awaitable[int]]


class _LiveDeliveryBoundary(Protocol):
    async def run(self, source_uri: str, consumer: object) -> object: ...


class _WindowsOperatorBoundary(Protocol):
    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def start(self, streams: Sequence[MixedLiveStream]) -> WindowsOperatorRuntimeSnapshot: ...

    async def wait(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def close(self) -> WindowsOperatorRuntimeSnapshot: ...


LiveDeliveryFactory = Callable[[int], _LiveDeliveryBoundary]
WindowsOperatorFactory = Callable[[ViewportLayout], _WindowsOperatorBoundary]


def _sanitize_stream_token(value: str) -> str:
    token = value.strip()
    if (
        not token
        or len(token) > _MAX_STREAM_TOKEN_LENGTH
        or not token.isascii()
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
            for character in token
        )
    ):
        raise ValueError("operator stream token is invalid")
    return token


async def _probe_dynamic_payload_type(source_uri: str) -> int:
    """Read one transient RTP header and return only its dynamic payload type."""
    payload_type: int | None = None

    async def capture(packet: memoryview) -> None:
        nonlocal payload_type
        candidate = int(packet[1] & 0x7F)
        if not 96 <= candidate <= 127:
            raise ValueError("operator RTP payload type is outside the supported range")
        payload_type = candidate

    delivery = EphemeralRtpDelivery(
        packet_goal=1,
        delivery_timeout_seconds=15.0,
        consumer_timeout_seconds=2.0,
        relay_startup_probe_seconds=0.5,
    )
    await delivery.deliver(source_uri, capture)
    if payload_type is None:
        raise RuntimeError("operator RTP payload type could not be established")
    return payload_type


class PrivateStageOneSourceResolver(OperatorSourceResolver):
    """Resolve one configured physical source without exposing private material."""

    __slots__ = (
        "_source_uri",
        "_credential_bundle",
        "_stream_token",
        "_payload_type",
        "_credential_probe",
        "_payload_probe",
    )

    def __init__(
        self,
        source_uri: str,
        credential_bundle: str,
        *,
        stream_token: str = _DEFAULT_STREAM_TOKEN,
        payload_type: int | None = _DEFAULT_PAYLOAD_TYPE,
        credential_probe: CredentialProbe = resolve_credential_index,
        payload_probe: PayloadTypeProbe = _probe_dynamic_payload_type,
    ) -> None:
        if not isinstance(source_uri, str) or not source_uri.strip():
            raise ValueError("private operator source is unavailable")
        if not isinstance(credential_bundle, str) or not credential_bundle.strip():
            raise ValueError("private operator credential bundle is unavailable")
        if payload_type is not None and (
            isinstance(payload_type, bool) or not 96 <= payload_type <= 127
        ):
            raise ValueError("operator RTP payload type must be between 96 and 127")
        if not callable(credential_probe):
            raise TypeError("credential_probe must be callable")
        if not callable(payload_probe):
            raise TypeError("payload_probe must be callable")

        self._source_uri = source_uri.strip()
        self._credential_bundle = credential_bundle
        self._stream_token = _sanitize_stream_token(stream_token)
        self._payload_type = payload_type
        self._credential_probe = credential_probe
        self._payload_probe = payload_probe

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        """Resolve the configured source only for the selected enrolled device."""
        if not isinstance(device, Device):
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            )
        if stream_token != self._stream_token:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            )

        try:
            parsed = urlsplit(self._source_uri)
            source_host = parsed.hostname
        except ValueError:
            source_host = None
        if source_host is None or source_host.casefold() != str(device.host).casefold():
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved live source is outside the selected device scope",
            )

        try:
            credential_index = await asyncio.to_thread(
                self._credential_probe,
                self._source_uri,
                self._credential_bundle,
            )
            if credential_index is None:
                raise ValueError("no credential candidate authenticated")
            authenticated_source = selected_source_uri(
                self._source_uri,
                self._credential_bundle,
                credential_index,
            )
            payload_type = self._payload_type
            if payload_type is None:
                payload_type = await self._payload_probe(authenticated_source)
            if isinstance(payload_type, bool) or not 96 <= payload_type <= 127:
                raise ValueError("operator RTP payload type is invalid")
        except OperatorLaunchError:
            raise
        except Exception:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            ) from None

        return ResolvedLiveSource(authenticated_source, payload_type)


def _default_delivery_factory(payload_type: int) -> _LiveDeliveryBoundary:
    """Reuse the physically qualified Stage-32/35 live delivery envelope."""
    return BoundedLivePresentationDelivery(
        payload_type,
        packet_goal=2048,
        delivery_timeout_seconds=30.0,
        packet_consumer_timeout_seconds=4.0,
        decoder_timeout_seconds=2.0,
        frame_consumer_timeout_seconds=2.0,
        cleanup_timeout_seconds=2.0,
        relay_startup_probe_seconds=0.5,
    )


def _stage_one_presentation_runtime_factory(
    bindings: Sequence[ViewportBinding],
) -> BoundedPresentationRuntime:
    """Reuse the physically qualified Stage-32 presentation timing/bounds."""
    return BoundedPresentationRuntime(
        bindings,
        max_streams=16,
        max_viewports=16,
        max_total_frames=100_000,
        max_total_frame_bytes=16 * 1024 * 1024 * 1024,
        consumer_timeout_seconds=2.0,
        stop_timeout_seconds=5.0,
        allow_all_live=True,
    )


def _default_windows_operator_factory(layout: ViewportLayout) -> _WindowsOperatorBoundary:
    return BoundedWindowsOperatorRuntime(
        layout,
        allow_single_live=True,
        presentation_runtime_factory=_stage_one_presentation_runtime_factory,
    )


class WindowsSingleLiveOperatorLauncher(OperatorLauncher):
    """Drive one private RTSP source through one visible accepted Windows viewport."""

    def __init__(
        self,
        *,
        delivery_factory: LiveDeliveryFactory = _default_delivery_factory,
        runtime_factory: WindowsOperatorFactory = _default_windows_operator_factory,
        detection_provider: AnalyticsObservationProvider | None = None,
    ) -> None:
        if not callable(delivery_factory):
            raise TypeError("delivery_factory must be callable")
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable")
        if detection_provider is not None and not callable(detection_provider):
            raise TypeError("detection_provider must be callable")
        self._delivery_factory = delivery_factory
        self._runtime_factory = runtime_factory
        self._detection_provider = detection_provider

    async def run(
        self,
        source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
    ) -> OperatorLaunchMetrics:
        if not isinstance(source, ResolvedLiveSource):
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.LAUNCH_FAILURE,
                "live operator runtime failed",
            )
        analytics_delivery: BoundedAnalyticsOverlayDelivery | None = None
        try:
            layout = ViewportLayout(
                placements=(
                    ViewportPlacement(
                        logical_slot=0,
                        geometry=ViewportGeometry(x=0, y=0, width=width, height=height),
                    ),
                )
            )
            delivery = self._delivery_factory(source.payload_type)
            if self._detection_provider is not None:
                analytics_delivery = BoundedAnalyticsOverlayDelivery(
                    delivery,
                    self._detection_provider,
                )
                delivery = analytics_delivery
            runtime = self._runtime_factory(layout)
            stream = MixedLiveStream(slot=0, source_uri=source.source_uri, delivery=delivery)
        except Exception:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.LAUNCH_FAILURE,
                "live operator runtime failed",
            ) from None

        primary_error: BaseException | None = None
        try:
            await runtime.start((stream,))
            final = await runtime.wait()
            if final.state is not WindowsOperatorRuntimeState.COMPLETE:
                raise OperatorLaunchError(
                    OperatorLaunchErrorCode.LAUNCH_FAILURE,
                    "live operator runtime failed",
                )
            analytics_snapshot = None if analytics_delivery is None else analytics_delivery.snapshot
            return OperatorLaunchMetrics(
                delivered_frames=final.delivered_frames,
                presentations=final.presentations,
                processed_controls=0,
                analytics_enabled=analytics_delivery is not None,
                analytics_provider_submissions=(
                    0 if analytics_snapshot is None else analytics_snapshot.provider_submissions
                ),
                analytics_provider_completions=(
                    0 if analytics_snapshot is None else analytics_snapshot.provider_completions
                ),
                analytics_failures=(
                    0 if analytics_snapshot is None else analytics_snapshot.analytics_failures
                ),
                analytics_rendered_boxes=(
                    0 if analytics_snapshot is None else analytics_snapshot.rendered_boxes
                ),
            )
        except asyncio.CancelledError as exc:
            primary_error = exc
            raise
        except OperatorLaunchError as exc:
            primary_error = exc
            raise
        except Exception as exc:
            primary_error = exc
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.LAUNCH_FAILURE,
                "live operator runtime failed",
            ) from None
        finally:
            try:
                await runtime.close()
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
            except Exception:
                if primary_error is None:
                    raise OperatorLaunchError(
                        OperatorLaunchErrorCode.LAUNCH_FAILURE,
                        "live operator runtime cleanup failed",
                    ) from None


def build_environment_operator_runtime(
    environment: Mapping[str, str],
    *,
    credential_probe: CredentialProbe = resolve_credential_index,
    payload_probe: PayloadTypeProbe = _probe_dynamic_payload_type,
    detection_provider: AnalyticsObservationProvider | None = None,
) -> tuple[OperatorSourceResolver | None, OperatorLauncher | None]:
    """Build the physical Stage-One bridge only when private configuration is complete."""
    source_uri = environment.get(STAGE_ONE_SOURCE_ENV, "").strip()
    credential_bundle = environment.get(STAGE_ONE_CREDENTIAL_ENV, "")
    if not source_uri or not credential_bundle.strip():
        return None, None

    stream_token = environment.get(STAGE_ONE_STREAM_TOKEN_ENV, _DEFAULT_STREAM_TOKEN)
    payload_raw = environment.get(STAGE_ONE_PAYLOAD_TYPE_ENV)
    try:
        payload_type = None if payload_raw is None or not payload_raw.strip() else int(payload_raw)
        resolver = PrivateStageOneSourceResolver(
            source_uri,
            credential_bundle,
            stream_token=stream_token,
            payload_type=payload_type,
            credential_probe=credential_probe,
            payload_probe=payload_probe,
        )
        launcher = WindowsSingleLiveOperatorLauncher(detection_provider=detection_provider)
    except (TypeError, ValueError):
        return None, None

    return resolver, launcher
