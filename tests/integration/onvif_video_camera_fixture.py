"""Real loopback ONVIF encoder controls for generated-media qualification."""

from __future__ import annotations

import copy
import importlib.util
import threading
from pathlib import Path

_PATH = Path(__file__).with_name("onvif_camera_fixture.py")
_SPEC = importlib.util.spec_from_file_location("k5_video_camera_base", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_BASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BASE)
_MEDIA, _TT = _BASE._MEDIA, _BASE._SCHEMA
_PROFILE, _ENCODER, _SOURCE = "generated-h264-profile", "generated-h264-encoder", "generated-source"


class OnvifVideoCameraFixture(_BASE.OnvifCameraFixture):
    """Verify real WSSE requests and apply writes to an owned generated publisher.

    The callback receives only finite encoder values, never credentials. It must
    change the generated publisher before Set acknowledges success. External
    codec changes are fixture-only, never performed through the product API.
    """

    def __init__(self, *, host, stream_uri, on_change=None):
        self._video_lock = threading.RLock()
        self._configuration = {
            "codec": "H264",
            "resolution": {"width": 320, "height": 240},
            "frame_rate": 15,
            "bitrate_kbps": 512,
            "gop": 15,
            "quality": 2.0,
            "encoding_interval": 1,
        }
        self._on_change = on_change
        self._use_count = 1
        self._mode = "normal"
        self._writes = []
        self._persistence_verified = True
        self._preserved_fields = True
        super().__init__(host=host, stream_uri=stream_uri)

    def current(self):
        with self._video_lock:
            return copy.deepcopy(self._configuration)

    def external_codec(self, codec):
        if codec not in {"H264", "MPEG4"}:
            raise ValueError("Generated external codec was refused")
        with self._video_lock:
            value = copy.deepcopy(self._configuration)
            value["codec"] = codec
            if self._on_change is not None:
                self._on_change(value)
            self._configuration = value

    def shared(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Generated shared-state flag was refused")
        with self._video_lock:
            self._use_count = 2 if enabled else 1

    def error_mode(self, value):
        if value not in {"normal", "malformed_options"}:
            raise ValueError("Generated response mode was refused")
        with self._video_lock:
            self._mode = value

    def evidence(self):
        with self._lock, self._video_lock:
            return {
                "get_configuration_requests": self._authenticated_requests.get(
                    "GetVideoEncoderConfiguration", 0
                ),
                "get_options_requests": self._authenticated_requests.get(
                    "GetVideoEncoderConfigurationOptions", 0
                ),
                "set_configuration_requests": len(self._writes),
                "force_persistence_verified": self._persistence_verified,
                "nonedited_fields_preserved": self._preserved_fields,
                "written_codecs": [value["codec"] for value in self._writes],
            }

    def _configuration_xml(self, *, tag="trt:Configuration"):
        value = self._configuration
        codec = value["codec"]
        special = (
            f"<tt:H264><tt:GovLength>{value['gop']}</tt:GovLength>"
            "<tt:H264Profile>Baseline</tt:H264Profile></tt:H264>"
            if codec == "H264"
            else f"<tt:MPEG4><tt:GovLength>{value['gop']}</tt:GovLength>"
            "<tt:Mpeg4Profile>SP</tt:Mpeg4Profile></tt:MPEG4>"
        )
        return (
            f'<{tag} token="{_ENCODER}"><tt:Name>Generated encoder</tt:Name>'
            f"<tt:UseCount>{self._use_count}</tt:UseCount><tt:Encoding>{codec}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{value['resolution']['width']}</tt:Width>"
            f"<tt:Height>{value['resolution']['height']}</tt:Height></tt:Resolution>"
            f"<tt:Quality>{value['quality']}</tt:Quality><tt:RateControl>"
            f"<tt:FrameRateLimit>{value['frame_rate']}</tt:FrameRateLimit>"
            f"<tt:EncodingInterval>{value['encoding_interval']}</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{value['bitrate_kbps']}</tt:BitrateLimit></tt:RateControl>"
            f"{special}<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
            "<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address><tt:Port>0</tt:Port>"
            "<tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            f"<tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>"
        )

    @staticmethod
    def _number(parent, name, *, integer=True):
        raw = parent.findtext(f"{{{_TT}}}{name}")
        if raw is None:
            raise ValueError
        number = float(raw)
        if integer and number != int(number):
            raise ValueError
        return int(number) if integer else number

    def _read_write(self, request):
        config = request.find(f"{{{_MEDIA}}}Configuration")
        if config is None or config.get("token") != _ENCODER:
            raise ValueError
        codec = config.findtext(f"{{{_TT}}}Encoding")
        if codec != "H264":
            raise ValueError
        resolution = config.find(f"{{{_TT}}}Resolution")
        rate, h264 = config.find(f"{{{_TT}}}RateControl"), config.find(f"{{{_TT}}}H264")
        if any(value is None for value in (resolution, rate, h264)):
            raise ValueError
        value = {
            "codec": codec,
            "resolution": {
                "width": self._number(resolution, "Width"),
                "height": self._number(resolution, "Height"),
            },
            "frame_rate": self._number(rate, "FrameRateLimit"),
            "bitrate_kbps": self._number(rate, "BitrateLimit"),
            "encoding_interval": self._number(rate, "EncodingInterval"),
            "gop": self._number(h264, "GovLength"),
            "quality": self._number(config, "Quality", integer=False),
        }
        if (
            value["resolution"]
            not in ({"width": 320, "height": 240}, {"width": 640, "height": 360})
            or not 1 <= value["frame_rate"] <= 30
            or not 128 <= value["bitrate_kbps"] <= 4096
            or not 1 <= value["gop"] <= 60
            or not 1 <= value["quality"] <= 6
            or value["encoding_interval"] != 1
        ):
            raise ValueError
        persistence = request.findtext(f"{{{_MEDIA}}}ForcePersistence") == "true"
        preserved = (
            config.findtext(f"{{{_TT}}}Name") == "Generated encoder"
            and config.findtext(f"{{{_TT}}}UseCount") == str(self._use_count)
            and config.findtext(f"{{{_TT}}}SessionTimeout") == "PT60S"
            and config.findtext(f"{{{_TT}}}Multicast/{{{_TT}}}Address/{{{_TT}}}IPv4Address")
            == "0.0.0.0"
            and h264.findtext(f"{{{_TT}}}H264Profile") == "Baseline"
        )
        if not persistence or not preserved:
            raise ValueError
        if self._on_change is not None:
            self._on_change(value)
        self._configuration = copy.deepcopy(value)
        self._writes.append(copy.deepcopy(value))
        self._persistence_verified &= persistence
        self._preserved_fields &= preserved

    def _response(self, action, request, path):
        if path != "/onvif/media_service":
            return super()._response(action, request, path)
        with self._video_lock:
            if action == "GetProfiles":
                return (
                    f'<trt:GetProfilesResponse><trt:Profiles token="{_PROFILE}" fixed="true">'
                    "<tt:Name>Generated video profile</tt:Name>"
                    '<tt:VideoSourceConfiguration token="generated-source-configuration">'
                    "<tt:Name>Generated source</tt:Name><tt:UseCount>1</tt:UseCount>"
                    f"<tt:SourceToken>{_SOURCE}</tt:SourceToken>"
                    '<tt:Bounds x="0" y="0" width="640" height="360"/>'
                    "</tt:VideoSourceConfiguration>"
                    + self._configuration_xml(tag="tt:VideoEncoderConfiguration")
                    + "</trt:Profiles></trt:GetProfilesResponse>"
                )
            if action == "GetVideoEncoderConfiguration":
                if request.findtext(f"{{{_MEDIA}}}ConfigurationToken") != _ENCODER:
                    return None
                return (
                    "<trt:GetVideoEncoderConfigurationResponse>"
                    + self._configuration_xml()
                    + "</trt:GetVideoEncoderConfigurationResponse>"
                )
            if action == "GetVideoEncoderConfigurationOptions":
                if (
                    request.findtext(f"{{{_MEDIA}}}ConfigurationToken") != _ENCODER
                    or request.findtext(f"{{{_MEDIA}}}ProfileToken") != _PROFILE
                ):
                    return None
                if self._mode == "malformed_options":
                    return (
                        "<trt:GetVideoEncoderConfigurationOptionsResponse>"
                        "<trt:Options><tt:QualityRange><tt:Min>8</tt:Min><tt:Max>1</tt:Max>"
                        "</tt:QualityRange><tt:H264/></trt:Options>"
                        "</trt:GetVideoEncoderConfigurationOptionsResponse>"
                    )
                return (
                    "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
                    "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>6</tt:Max></tt:QualityRange>"
                    "<tt:H264><tt:ResolutionsAvailable><tt:Width>320</tt:Width>"
                    "<tt:Height>240</tt:Height></tt:ResolutionsAvailable>"
                    "<tt:ResolutionsAvailable><tt:Width>640</tt:Width><tt:Height>360</tt:Height>"
                    "</tt:ResolutionsAvailable><tt:GovLengthRange><tt:Min>1</tt:Min>"
                    "<tt:Max>60</tt:Max></tt:GovLengthRange><tt:FrameRateRange>"
                    "<tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>"
                    "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max>"
                    "</tt:EncodingIntervalRange><tt:H264ProfilesSupported>Baseline</tt:H264ProfilesSupported>"
                    "</tt:H264><tt:MPEG4><tt:ResolutionsAvailable><tt:Width>640</tt:Width>"
                    "<tt:Height>360</tt:Height></tt:ResolutionsAvailable></tt:MPEG4>"
                    "<tt:Extension><tt:H264><tt:BitrateRange><tt:Min>128</tt:Min>"
                    "<tt:Max>4096</tt:Max></tt:BitrateRange></tt:H264></tt:Extension>"
                    "</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
                )
            if action == "SetVideoEncoderConfiguration":
                try:
                    self._read_write(request)
                except (ValueError, TypeError, OSError, RuntimeError):
                    return None
                return "<trt:SetVideoEncoderConfigurationResponse/>"
        return super()._response(action, request, path)
