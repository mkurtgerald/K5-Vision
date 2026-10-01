from __future__ import annotations

import asyncio

import pytest

import k5vision.operator_runtime as operator_runtime_module
from k5vision.domain.devices import Device, DeviceProtocol
from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
from k5vision.operator_launch import OperatorLaunchError, OperatorLaunchErrorCode
from k5vision.operator_runtime import (
    LocalTestSourceResolver,
    PublicTestSourceResolver,
    WindowsSingleLiveOperatorLauncher,
    build_environment_operator_runtime,
    resolve_public_test_source_ip,
)


def _public_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(2, 1, 6, "", ("8.8.8.8", 1935))]


def test_public_test_source_resolves_only_global_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", _public_dns)

    assert resolve_public_test_source_ip("rtsp://stream.example.test:1935/app/live") == "8.8.8.8"


@pytest.mark.parametrize(
    "answers",
    [
        [(2, 1, 6, "", ("10.0.0.8", 554))],
        [
            (2, 1, 6, "", ("8.8.8.8", 554)),
            (2, 1, 6, "", ("127.0.0.1", 554)),
        ],
    ],
)
def test_public_test_source_rejects_non_public_dns(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[object, ...]],
) -> None:
    monkeypatch.setattr(
        operator_runtime_module,
        "getaddrinfo",
        lambda *_args, **_kwargs: answers,
    )

    with pytest.raises(ValueError, match="public addresses"):
        resolve_public_test_source_ip("rtsp://stream.example.test/live")


@pytest.mark.parametrize(
    "source",
    [
        "rtsp://user:secret@stream.example.test/live",
        "rtsps://stream.example.test/live",
        "http://stream.example.test/live",
        "rtsp://127.0.0.1/live",
    ],
)
def test_public_test_source_rejects_credentials_or_unsafe_scheme(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", _public_dns)

    with pytest.raises(ValueError):
        resolve_public_test_source_ip(source)


def test_public_test_resolver_preserves_hostname_and_binds_enrolled_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", _public_dns)
    probed: list[str] = []

    async def payload_probe(source_uri: str) -> int:
        probed.append(source_uri)
        return 97

    resolver = PublicTestSourceResolver(
        "rtsp://stream.example.test:1935/app/live?profile=main",
        "8.8.8.8",
        payload_probe=payload_probe,
    )
    device = Device(
        name="Public test",
        host="8.8.8.8",
        management_port=1935,
        protocols={DeviceProtocol.RTSP},
        tags={"alpha-public-test"},
    )

    resolved = asyncio.run(resolver.resolve(device, "public-test"))

    assert resolved.source_uri == "rtsp://stream.example.test:1935/app/live?profile=main"
    assert resolved.payload_type == 97
    assert resolved.endpoint_ip == "8.8.8.8"
    assert probed == [resolved.source_uri]


def test_local_test_source_is_loopback_only() -> None:
    async def payload_probe(source_uri: str) -> int:
        assert source_uri == "rtsp://127.0.0.1:8554/k5synthetic"
        return 96

    resolver = LocalTestSourceResolver(
        "rtsp://127.0.0.1:8554/k5synthetic",
        payload_probe=payload_probe,
    )
    device = Device(
        name="Synthetic test",
        host="127.0.0.1",
        management_port=8554,
        protocols={DeviceProtocol.RTSP},
        tags={"alpha-local-synthetic"},
    )

    resolved = asyncio.run(resolver.resolve(device, "local-test"))

    assert resolved.source_uri == "rtsp://127.0.0.1:8554/k5synthetic"
    assert resolved.payload_type == 96


@pytest.mark.parametrize(
    "source",
    [
        "rtsp://192.168.1.10:8554/k5synthetic",
        "rtsp://8.8.8.8:8554/k5synthetic",
        "rtsp://user:secret@127.0.0.1:8554/k5synthetic",
        "http://127.0.0.1:8554/k5synthetic",
    ],
)
def test_local_test_source_rejects_non_loopback_or_credentials(source: str) -> None:
    with pytest.raises(ValueError):
        LocalTestSourceResolver(source, payload_type=96)


def test_local_test_environment_builder_is_mutually_exclusive() -> None:
    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_LOCAL_TEST_RTSP_SOURCE": "rtsp://127.0.0.1:8554/k5synthetic",
            "K5_OPERATOR_STREAM_TOKEN": "local-test",
            "K5_OPERATOR_RTP_PAYLOAD_TYPE": "96",
        }
    )

    assert isinstance(resolver, LocalTestSourceResolver)
    assert isinstance(launcher, WindowsSingleLiveOperatorLauncher)
    delivery = launcher._delivery_factory(96)
    assert isinstance(delivery, GStreamerDirectFrameDelivery)
    assert delivery._frame_goal == 30
    assert delivery._delivery_timeout_seconds == 45.0
    assert delivery._startup_probe_ms == 5_000
    assert delivery._startup_probe_ms == 500

    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_LOCAL_TEST_RTSP_SOURCE": "rtsp://127.0.0.1:8554/k5synthetic",
            "K5_PUBLIC_TEST_RTSP_SOURCE": "rtsp://stream.example.test/live",
            "K5_PUBLIC_TEST_SOURCE_IP": "8.8.8.8",
        }
    )
    assert resolver is None
    assert launcher is None


def test_public_test_environment_builder_keeps_private_mode_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", _public_dns)
    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_PUBLIC_TEST_RTSP_SOURCE": "rtsp://stream.example.test:1935/app/live",
            "K5_PUBLIC_TEST_SOURCE_IP": "8.8.8.8",
            "K5_OPERATOR_STREAM_TOKEN": "public-test",
            "K5_OPERATOR_RTP_PAYLOAD_TYPE": "96",
        }
    )

    assert isinstance(resolver, PublicTestSourceResolver)
    assert isinstance(launcher, WindowsSingleLiveOperatorLauncher)
    delivery = launcher._delivery_factory(96)
    assert isinstance(delivery, GStreamerDirectFrameDelivery)
    assert delivery._frame_goal == 225
    assert delivery._delivery_timeout_seconds == 25.0

    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_PUBLIC_TEST_RTSP_SOURCE": "rtsp://stream.example.test:1935/app/live",
            "K5_PUBLIC_TEST_SOURCE_IP": "8.8.8.8",
            "K5_STAGE03_SOURCE": "rtsp://192.0.2.10/live",
            "K5_STAGE03_CAM_CRED": "operator\nsecret\n",
        }
    )
    assert resolver is None
    assert launcher is None


def test_public_test_environment_builder_skips_udp_payload_probe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", _public_dns)
    probed: list[str] = []

    async def payload_probe(source_uri: str) -> int:
        probed.append(source_uri)
        raise AssertionError("public direct RTSP/TCP acceptance should not probe RTP over UDP")

    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_PUBLIC_TEST_RTSP_SOURCE": "rtsp://stream.example.test:1935/app/live",
            "K5_PUBLIC_TEST_SOURCE_IP": "8.8.8.8",
            "K5_OPERATOR_STREAM_TOKEN": "public-test",
        },
        payload_probe=payload_probe,
    )

    assert isinstance(resolver, PublicTestSourceResolver)
    assert isinstance(launcher, WindowsSingleLiveOperatorLauncher)

    device = Device(
        name="Public test",
        host="8.8.8.8",
        management_port=1935,
        protocols={DeviceProtocol.RTSP},
        tags={"alpha-public-test"},
    )
    resolved = asyncio.run(resolver.resolve(device, "public-test"))

    assert resolved.payload_type == 96
    assert probed == []
    delivery = launcher._delivery_factory(resolved.payload_type)
    assert isinstance(delivery, GStreamerDirectFrameDelivery)
    assert delivery._frame_goal == 30
    assert delivery._delivery_timeout_seconds == 45.0
    assert delivery._startup_probe_ms == 5_000


def test_public_test_resolver_revalidates_pinned_ip_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [("8.8.8.8", 1935)]

    def dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", answer) for answer in answers]

    monkeypatch.setattr(operator_runtime_module, "getaddrinfo", dns)
    resolver = PublicTestSourceResolver(
        "rtsp://stream.example.test:1935/app/live",
        "8.8.8.8",
        payload_type=96,
    )
    device = Device(
        name="Public test",
        host="8.8.8.8",
        management_port=1935,
        protocols={DeviceProtocol.RTSP},
        tags={"alpha-public-test"},
    )

    answers[:] = [("1.1.1.1", 1935)]
    with pytest.raises(OperatorLaunchError) as caught:
        asyncio.run(resolver.resolve(device, "public-test"))

    assert caught.value.code == OperatorLaunchErrorCode.SOURCE_UNAVAILABLE
