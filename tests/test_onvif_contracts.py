from k5vision.adapters.onvif_contracts import (
    OnvifDeviceSnapshot,
    OnvifProfileSnapshot,
    normalize_onvif_snapshot,
)
from k5vision.domain.streams import StreamRole, VideoCodec


def test_onvif_snapshot_normalizes_identity_and_profiles() -> None:
    snapshot = OnvifDeviceSnapshot(
        manufacturer="ExampleCam",
        model="XC-4K",
        firmware="1.2.3",
        serial_number="ABC123",
        supports_ptz=True,
        supports_events=True,
        profiles=[
            OnvifProfileSnapshot(
                token="sub",
                name="Sub Stream",
                encoding="H.264",
                width=640,
                height=360,
                fps=10,
                bitrate_kbps=512,
            ),
            OnvifProfileSnapshot(
                token="main",
                name="Main Stream",
                encoding="H265",
                width=3840,
                height=2160,
                fps=30,
                bitrate_kbps=8192,
            ),
        ],
    )

    capabilities = normalize_onvif_snapshot(snapshot)

    assert capabilities.identity.manufacturer == "ExampleCam"
    assert capabilities.supports_ptz is True
    assert capabilities.stream_profiles[0].token == "main"
    assert capabilities.stream_profiles[0].role is StreamRole.MAIN
    assert capabilities.stream_profiles[0].codec is VideoCodec.H265
    assert capabilities.stream_profiles[1].token == "sub"
    assert capabilities.stream_profiles[1].role is StreamRole.SUBSTREAM


def test_middle_profiles_are_auxiliary_and_unknown_codec_is_preserved() -> None:
    snapshot = OnvifDeviceSnapshot(
        profiles=[
            OnvifProfileSnapshot(token="a", name="4K", encoding="H265", width=3840, height=2160),
            OnvifProfileSnapshot(token="b", name="1080p", encoding="AV1", width=1920, height=1080),
            OnvifProfileSnapshot(token="c", name="360p", encoding="H264", width=640, height=360),
        ]
    )

    capabilities = normalize_onvif_snapshot(snapshot)

    assert capabilities.stream_profiles[1].role is StreamRole.AUXILIARY
    assert capabilities.stream_profiles[1].codec is VideoCodec.UNKNOWN
    assert capabilities.stream_profiles[2].role is StreamRole.SUBSTREAM


def test_single_profile_is_main() -> None:
    snapshot = OnvifDeviceSnapshot(
        profiles=[
            OnvifProfileSnapshot(token="only", name="Only", encoding="JPEG", width=1280, height=720)
        ]
    )

    capabilities = normalize_onvif_snapshot(snapshot)

    assert len(capabilities.stream_profiles) == 1
    assert capabilities.stream_profiles[0].role is StreamRole.MAIN
    assert capabilities.stream_profiles[0].codec is VideoCodec.MJPEG
