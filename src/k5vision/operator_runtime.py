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
from ipaddress import ip_address
from socket import SOCK_STREAM, getaddrinfo
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from k5vision.analytics_runtime import OwnedAnalyticsProvider
from k5vision.domain.devices import Device
from k5vision.media.analytics_overlay_delivery import (
    AnalyticsObservationProvider,
    BoundedAnalyticsOverlayDelivery,
)
from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
from k5vision.media.live_presentation import BoundedLivePresentationDelivery
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_runtime import BoundedPresentationRuntime
from k5vision.media.rtp_delivery import EphemeralRtpDelivery
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeError,
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
PUBLIC_TEST_SOURCE_ENV = "K5_PUBLIC_TEST_RTSP_SOURCE"
PUBLIC_TEST_SOURCE_IP_ENV = "K5_PUBLIC_TEST_SOURCE_IP"
LOCAL_TEST_SOURCE_ENV = "K5_LOCAL_TEST_RTSP_SOURCE"
_DEFAULT_STREAM_TOKEN = "main"
_PUBLIC_TEST_STREAM_TOKEN = "public-test"
_LOCAL_TEST_STREAM_TOKEN = "local-test"
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
AnalyticsProviderFactory = Callable[[], Awaitable[OwnedAnalyticsProvider]]


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


def _public_test_source_addresses(source_uri: str) -> tuple[SplitResult, tuple[str, ...]]:
    """Validate a credential-free public RTSP URI and return only global endpoints."""
    if not isinstance(source_uri, str) or not source_uri.strip():
        raise ValueError("public RTSP test source is unavailable")
    try:
        parsed = urlsplit(source_uri.strip())
        port = parsed.port or 554
    except ValueError as exc:
        raise ValueError("public RTSP test source is invalid") from exc
    if (
        parsed.scheme.casefold() != "rtsp"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError("public RTSP test source is invalid")

    addresses = []
    try:
        addresses = [ip_address(parsed.hostname)]
    except ValueError:
        try:
            answers = getaddrinfo(parsed.hostname, port, type=SOCK_STREAM)
        except OSError as exc:
            raise ValueError("public RTSP test source could not be resolved") from exc
        for answer in answers:
            try:
                addresses.append(ip_address(answer[4][0]))
            except (ValueError, IndexError):
                continue

    unique = sorted(set(addresses), key=lambda address: (address.version != 4, int(address)))
    if not unique or any(not address.is_global for address in unique):
        raise ValueError("public RTSP test source must resolve only to public addresses")
    return parsed, tuple(str(address) for address in unique)


def resolve_public_test_source_ip(source_uri: str) -> str:
    """Resolve a public alpha-test RTSP source to one deterministic global address."""
    _, addresses = _public_test_source_addresses(source_uri)
    return addresses[0]


def _rewrite_public_test_source(parsed: SplitResult, source_ip: str) -> str:
    address = ip_address(source_ip)
    host = f"[{address}]" if address.version == 6 else str(address)
    port = parsed.port or 554
    return urlunsplit(("rtsp", f"{host}:{port}", parsed.path, parsed.query, ""))


def _local_test_source(source_uri: str) -> tuple[str, str]:
    """Validate one credential-free loopback-only RTSP alpha source."""
    if not isinstance(source_uri, str) or not source_uri.strip():
        raise ValueError("local RTSP test source is unavailable")
    try:
        parsed = urlsplit(source_uri.strip())
        port = parsed.port or 554
        host = ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ValueError("local RTSP test source is invalid") from exc
    if (
        parsed.scheme.casefold() != "rtsp"
        or not host.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError("local RTSP test source must be credential-free loopback RTSP")
    normalized = urlunsplit(
        (
            "rtsp",
            f"[{host}]:{port}" if host.version == 6 else f"{host}:{port}",
            parsed.path,
            parsed.query,
            "",
        )
    )
    return normalized, str(host)


class LocalTestSourceResolver(OperatorSourceResolver):
    """Resolve one explicit loopback-only synthetic alpha stream."""

    __slots__ = ("_source_uri", "_source_ip", "_stream_token", "_payload_type", "_payload_probe")

    def __init__(
        self,
        source_uri: str,
        *,
        stream_token: str = _LOCAL_TEST_STREAM_TOKEN,
        payload_type: int | None = None,
        payload_probe: PayloadTypeProbe | None = None,
    ) -> None:
        normalized_uri, source_ip = _local_test_source(source_uri)
        if payload_type is not None and (
            isinstance(payload_type, bool) or not 96 <= payload_type <= 127
        ):
            raise ValueError("operator RTP payload type must be between 96 and 127")
        resolved_payload_probe = (
            _probe_dynamic_payload_type if payload_probe is None else payload_probe
        )
        if not callable(resolved_payload_probe):
            raise TypeError("payload_probe must be callable")
        self._source_uri = normalized_uri
        self._source_ip = source_ip
        self._stream_token = _sanitize_stream_token(stream_token)
        self._payload_type = payload_type
        self._payload_probe = resolved_payload_probe

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        """Return the synthetic stream only for the enrolled loopback test device."""
        if not isinstance(device, Device) or stream_token != self._stream_token:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            )
        if str(device.host) != self._source_ip:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved live source is outside the selected device scope",
            )
        try:
            payload_type = self._payload_type
            if payload_type is None:
                payload_type = await self._payload_probe(self._source_uri)
            if isinstance(payload_type, bool) or not 96 <= payload_type <= 127:
                raise ValueError("operator RTP payload type is invalid")
        except Exception:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            ) from None
        return ResolvedLiveSource(self._source_uri, payload_type)


class PublicTestSourceResolver(OperatorSourceResolver):
    """Resolve one credential-free public test stream to a pinned public address."""

    __slots__ = (
        "_source_uri",
        "_pinned_source_uri",
        "_source_ip",
        "_stream_token",
        "_payload_type",
        "_payload_probe",
    )

    def __init__(
        self,
        source_uri: str,
        source_ip: str,
        *,
        stream_token: str = _PUBLIC_TEST_STREAM_TOKEN,
        payload_type: int | None = None,
        payload_probe: PayloadTypeProbe | None = None,
    ) -> None:
        parsed, addresses = _public_test_source_addresses(source_uri)
        try:
            normalized_ip = str(ip_address(source_ip.strip()))
        except (AttributeError, ValueError) as exc:
            raise ValueError("public RTSP test source address is invalid") from exc
        if normalized_ip not in addresses:
            raise ValueError("public RTSP test source address does not match current DNS")
        if payload_type is not None and (
            isinstance(payload_type, bool) or not 96 <= payload_type <= 127
        ):
            raise ValueError("operator RTP payload type must be between 96 and 127")
        resolved_payload_probe = (
            _probe_dynamic_payload_type if payload_probe is None else payload_probe
        )
        if not callable(resolved_payload_probe):
            raise TypeError("payload_probe must be callable")

        self._source_uri = urlunsplit(("rtsp", parsed.netloc, parsed.path, parsed.query, ""))
        self._pinned_source_uri = _rewrite_public_test_source(parsed, normalized_ip)
        self._source_ip = normalized_ip
        self._stream_token = _sanitize_stream_token(stream_token)
        self._payload_type = payload_type
        self._payload_probe = resolved_payload_probe

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        """Return the pinned public stream only for its enrolled ephemeral device."""
        if not isinstance(device, Device) or stream_token != self._stream_token:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            )
        if str(device.host) != self._source_ip:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH,
                "resolved live source is outside the selected device scope",
            )
        try:
            _, current_addresses = _public_test_source_addresses(self._source_uri)
            if self._source_ip not in current_addresses:
                raise ValueError("public RTSP test source address changed")
            payload_type = self._payload_type
            if payload_type is None:
                payload_type = await self._payload_probe(self._pinned_source_uri)
            if isinstance(payload_type, bool) or not 96 <= payload_type <= 127:
                raise ValueError("operator RTP payload type is invalid")
        except Exception:
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.SOURCE_UNAVAILABLE,
                "selected live source could not be resolved",
            ) from None
        return ResolvedLiveSource(
            self._pinned_source_uri,
            payload_type,
            endpoint_ip=self._source_ip,
        )


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


def _local_direct_rtsp_test_delivery_factory(_payload_type: int) -> _LiveDeliveryBoundary:
    """Use direct decoded RTSP/TCP frames for bounded local alpha acceptance."""
    return GStreamerDirectFrameDelivery(
        frame_goal=225,
        delivery_timeout_seconds=25.0,
        consumer_timeout_seconds=2.0,
        startup_probe_ms=500,
    )


def _public_direct_rtsp_test_delivery_factory(_payload_type: int) -> _LiveDeliveryBoundary:
    """Allow slower public RTSP startup while still proving real decoded video."""
    return GStreamerDirectFrameDelivery(
        frame_goal=30,
        delivery_timeout_seconds=45.0,
        consumer_timeout_seconds=2.0,
        startup_probe_ms=5_000,
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
        detection_provider_factory: AnalyticsProviderFactory | None = None,
    ) -> None:
        if not callable(delivery_factory):
            raise TypeError("delivery_factory must be callable")
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable")
        if detection_provider is not None and not callable(detection_provider):
            raise TypeError("detection_provider must be callable")
        if detection_provider_factory is not None and not callable(detection_provider_factory):
            raise TypeError("detection_provider_factory must be callable")
        if detection_provider is not None and detection_provider_factory is not None:
            raise ValueError("analytics provider and factory are mutually exclusive")
        self._detection_provider_factory = detection_provider_factory
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
        owned: OwnedAnalyticsProvider | None = None
        primary_error: BaseException | None = None
        try:
            if self._detection_provider_factory is not None:
                try:
                    owned = await self._detection_provider_factory()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise OperatorLaunchError(
                        OperatorLaunchErrorCode.LAUNCH_FAILURE,
                        "configured analytics could not start",
                    ) from None
            return await self._run(
                source,
                width=width,
                height=height,
                detection_provider=owned if owned is not None else self._detection_provider,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if owned is not None:
                try:
                    await owned.aclose()
                except asyncio.CancelledError:
                    if primary_error is None:
                        raise
                except Exception:
                    if primary_error is None:
                        raise OperatorLaunchError(
                            OperatorLaunchErrorCode.LAUNCH_FAILURE,
                            "configured analytics cleanup failed",
                        ) from None

    async def _run(
        self,
        source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
        detection_provider: AnalyticsObservationProvider | None,
    ) -> OperatorLaunchMetrics:
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
            if detection_provider is not None:
                analytics_delivery = BoundedAnalyticsOverlayDelivery(
                    delivery,
                    detection_provider,
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
        except WindowsOperatorRuntimeError as exc:
            primary_error = exc
            raise OperatorLaunchError(
                OperatorLaunchErrorCode.LAUNCH_FAILURE,
                f"live operator runtime failed at {exc.code.value}: {exc}",
            ) from None
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
    detection_provider_factory: AnalyticsProviderFactory | None = None,
) -> tuple[OperatorSourceResolver | None, OperatorLauncher | None]:
    """Build the physical Stage-One bridge only when private configuration is complete."""
    source_uri = environment.get(STAGE_ONE_SOURCE_ENV, "").strip()
    credential_bundle = environment.get(STAGE_ONE_CREDENTIAL_ENV, "")
    public_source_uri = environment.get(PUBLIC_TEST_SOURCE_ENV, "").strip()
    public_source_ip = environment.get(PUBLIC_TEST_SOURCE_IP_ENV, "").strip()
    local_source_uri = environment.get(LOCAL_TEST_SOURCE_ENV, "").strip()
    payload_raw = environment.get(STAGE_ONE_PAYLOAD_TYPE_ENV)

    if local_source_uri:
        if source_uri or credential_bundle.strip() or public_source_uri or public_source_ip:
            return None, None
        stream_token = environment.get(STAGE_ONE_STREAM_TOKEN_ENV, _LOCAL_TEST_STREAM_TOKEN)
        try:
            payload_type = (
                None if payload_raw is None or not payload_raw.strip() else int(payload_raw)
            )
            resolver = LocalTestSourceResolver(
                local_source_uri,
                stream_token=stream_token,
                payload_type=payload_type,
                payload_probe=payload_probe,
            )
            launcher = WindowsSingleLiveOperatorLauncher(
                delivery_factory=_local_direct_rtsp_test_delivery_factory,
                detection_provider=detection_provider,
                detection_provider_factory=detection_provider_factory,
            )
        except (TypeError, ValueError):
            return None, None
        return resolver, launcher

    if public_source_uri or public_source_ip:
        if source_uri or credential_bundle.strip() or not public_source_uri or not public_source_ip:
            return None, None
        stream_token = environment.get(STAGE_ONE_STREAM_TOKEN_ENV, _PUBLIC_TEST_STREAM_TOKEN)
        try:
            payload_type = (
                _DEFAULT_PAYLOAD_TYPE
                if payload_raw is None or not payload_raw.strip()
                else int(payload_raw)
            )
            resolver = PublicTestSourceResolver(
                public_source_uri,
                public_source_ip,
                stream_token=stream_token,
                payload_type=payload_type,
                payload_probe=payload_probe,
            )
            launcher = WindowsSingleLiveOperatorLauncher(
                delivery_factory=_public_direct_rtsp_test_delivery_factory,
                detection_provider=detection_provider,
                detection_provider_factory=detection_provider_factory,
            )
        except (TypeError, ValueError):
            return None, None
        return resolver, launcher

    if not source_uri or not credential_bundle.strip():
        return None, None

    stream_token = environment.get(STAGE_ONE_STREAM_TOKEN_ENV, _DEFAULT_STREAM_TOKEN)
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
        launcher = WindowsSingleLiveOperatorLauncher(
            detection_provider=detection_provider,
            detection_provider_factory=detection_provider_factory,
        )
    except (TypeError, ValueError):
        return None, None

    return resolver, launcher
