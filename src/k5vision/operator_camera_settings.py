"""Bounded ONVIF encoder inspection and verified configuration changes.

The existing reviewed ONVIF transport owns authentication, address pinning and
clock compensation. This module implements Media1/Media2 encoder operations;
configuration readback is deliberately separate from delivered-video proof.
Private inspection objects belong to the API's short-lived authorized vault.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

from k5vision.operator_onvif import (
    _DEVICE,
    _MEDIA,
    _MEDIA2,
    _TOKEN,
    OnvifConnector,
    OnvifError,
    _endpoint,
    _stream_uri,
)
from k5vision.operator_time_admin import _camera_client

_TT = "http://www.onvif.org/ver10/schema"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_MAX_PROFILES = 32
_FIELDS = frozenset(
    {"codec", "frame_rate", "resolution", "bitrate_kbps", "gop", "quality", "encoding_interval"}
)
_CODES = frozenset(
    {
        "video_unsupported",
        "video_auth_failed",
        "video_busy",
        "video_unreachable",
        "video_invalid_response",
        "video_invalid_request",
        "video_configuration_conflict",
        "video_shared_configuration",
        "video_outcome_unverified",
        "video_readback_mismatch",
        "video_codec_unsupported",
        "video_profile_ambiguous",
        "video_profile_unidentified",
        "video_inspection_expired",
        "video_closed",
    }
)


class CameraVideoSettingsError(RuntimeError):
    """A finite diagnostic; remote text and credentials never become errors."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "video_invalid_response"
        super().__init__(self.code)


def _error(code="video_invalid_response"):
    raise CameraVideoSettingsError(code)


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _child(parent, name, namespace=_TT, *, optional=False):
    values = [node for node in parent if node.tag == f"{{{namespace}}}{name}"]
    if len(values) > 1 or (not optional and not values):
        _error()
    return values[0] if values else None


def _text(parent, name, namespace=_TT, *, optional=False):
    item = _child(parent, name, namespace, optional=optional)
    return None if item is None else (item.text or "").strip()


def _token(value):
    if type(value) is not str or not _TOKEN.fullmatch(value):
        _error()
    return value


def _number(value, *, integer=False, minimum=0, maximum=1_000_000):
    try:
        if isinstance(value, str):
            if len(value) > 32:
                raise ValueError
            value = float(value)
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError
        if not minimum <= value <= maximum or (integer and int(value) != value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        _error()
    return int(value) if integer else float(value)


def _range(parent, name, *, integer=False, maximum=1_000_000):
    node = _child(parent, name, optional=True)
    if node is None:
        return None
    low = _number(_text(node, "Min"), integer=integer, maximum=maximum)
    high = _number(_text(node, "Max"), integer=integer, maximum=maximum)
    if high < low:
        _error()
    return {"min": low, "max": high}


def _codec(value):
    if type(value) is not str or len(value) > 32:
        _error()
    value = value.upper().replace(".", "").removeprefix("VIDEO/")
    return {"JPEG": "MJPEG", "HEVC": "H265"}.get(
        value, value if value in {"H264", "H265", "MJPEG", "MPEG4"} else "UNKNOWN"
    )


def _call(client, endpoint, namespace, action, operation):
    body = client.request(endpoint, operation, namespace, action)
    nodes = [node for node in body if node.tag == f"{{{namespace}}}{action}Response"]
    if len(nodes) != 1 or len(body) != 1:
        _error()
    return nodes[0]


def _uri_identity(uri):
    parsed = urlsplit(uri)
    return parsed.scheme, parsed.hostname, parsed.port or 554, parsed.path, parsed.query


def _stream_account(uri, source):
    """A management account never becomes the saved RTSP account implicitly."""
    parsed, original = urlsplit(uri), urlsplit(source.source_uri)
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    if parsed.port is not None:
        host += ":" + str(parsed.port)
    if original.username is not None:
        host = original.netloc.rsplit("@", 1)[0] + "@" + host
    result = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
    return _stream_uri(result, str(source.device.host), "", "")


def _media_candidates(client, device):
    base = device.rsplit("/onvif/device_service", 1)[0]
    endpoints = {}
    try:
        response = _call(
            client,
            device,
            _DEVICE,
            "GetServices",
            "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability>"
            "</tds:GetServices>",
        )
        for item in response:
            if item.tag != f"{{{_DEVICE}}}Service":
                continue
            namespace = _text(item, "Namespace", _DEVICE)
            if namespace in {_MEDIA, _MEDIA2}:
                if namespace in endpoints:
                    _error()
                endpoints[namespace] = _endpoint(_text(item, "XAddr", _DEVICE), client.host)
    except OnvifError as error:
        if error.code != "unsupported":
            raise
    if not endpoints:
        try:
            response = _call(
                client,
                device,
                _DEVICE,
                "GetCapabilities",
                "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
            )
            capabilities = _child(response, "Capabilities", _DEVICE)
            media = _child(capabilities, "Media", optional=True)
            if media is not None:
                endpoints[_MEDIA] = _endpoint(_text(media, "XAddr"), client.host)
        except OnvifError as error:
            if error.code != "unsupported":
                raise
    return [
        (namespace, endpoints.get(namespace, base + suffix))
        for namespace, suffix in (
            (_MEDIA, "/onvif/media_service"),
            (_MEDIA2, "/onvif/media2_service"),
        )
        if namespace in endpoints or not endpoints
    ]


@dataclass(frozen=True, repr=False)
class _Profile:
    token: str
    encoder: str
    source: str | None


def _profiles(client, endpoint, namespace):
    prefix = "trt" if namespace == _MEDIA else "tr2"
    operation = (
        "<trt:GetProfiles/>"
        if namespace == _MEDIA
        else "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>"
    )
    response = _call(client, endpoint, namespace, "GetProfiles", operation)
    nodes = [node for node in response if node.tag == f"{{{namespace}}}Profiles"]
    if not 1 <= len(nodes) <= _MAX_PROFILES:
        _error("video_unsupported")
    result = []
    seen = set()
    for node in nodes:
        token = _token(node.get("token"))
        if token in seen:
            _error()
        seen.add(token)
        if namespace == _MEDIA:
            encoder = _child(node, "VideoEncoderConfiguration", optional=True)
            source = _child(node, "VideoSourceConfiguration", optional=True)
        else:
            configurations = _child(node, "Configurations", namespace, optional=True)
            if configurations is None:
                continue
            encoder = _child(configurations, "VideoEncoder", namespace, optional=True)
            source = _child(configurations, "VideoSource", namespace, optional=True)
        if encoder is None:
            continue
        source_token = None if source is None else _text(source, "SourceToken", optional=True)
        if source_token is not None:
            source_token = _token(source_token)
        result.append(_Profile(token, _token(encoder.get("token")), source_token))
    if not result:
        _error("video_unsupported")
    return prefix, tuple(result)


def _uri(client, endpoint, namespace, profile):
    prefix = "trt" if namespace == _MEDIA else "tr2"
    setup = (
        "<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
        if namespace == _MEDIA
        else "<tr2:Protocol>RTSP</tr2:Protocol>"
    )
    response = _call(
        client,
        endpoint,
        namespace,
        "GetStreamUri",
        f"<{prefix}:GetStreamUri>{setup}<{prefix}:ProfileToken>{escape(profile.token)}"
        f"</{prefix}:ProfileToken></{prefix}:GetStreamUri>",
    )
    uri = (
        _text(_child(response, "MediaUri", namespace), "Uri")
        if namespace == _MEDIA
        else _text(response, "Uri", namespace)
    )
    return _stream_uri(uri, client.host, "", "")


def _configuration(client, endpoint, namespace, profile):
    prefix = "trt" if namespace == _MEDIA else "tr2"
    action = (
        "GetVideoEncoderConfiguration" if namespace == _MEDIA else "GetVideoEncoderConfigurations"
    )
    response = _call(
        client,
        endpoint,
        namespace,
        action,
        f"<{prefix}:{action}><{prefix}:ConfigurationToken>{escape(profile.encoder)}"
        f"</{prefix}:ConfigurationToken></{prefix}:{action}>",
    )
    name = "Configuration" if namespace == _MEDIA else "Configurations"
    nodes = [node for node in response if node.tag == f"{{{namespace}}}{name}"]
    if len(nodes) != 1 or nodes[0].get("token") != profile.encoder:
        _error()
    return copy.deepcopy(nodes[0])


def _normalize(configuration, namespace):
    codec = _codec(_text(configuration, "Encoding"))
    resolution = _child(configuration, "Resolution")
    rate = _child(configuration, "RateControl", optional=True)
    frame = bitrate = interval = gop = None
    if rate is not None:
        frame = _number(
            _text(rate, "FrameRateLimit"),
            integer=namespace == _MEDIA,
            minimum=0.001,
            maximum=1000,
        )
        bitrate = _number(_text(rate, "BitrateLimit"), integer=True)
        if namespace == _MEDIA:
            interval = _number(_text(rate, "EncodingInterval"), integer=True, minimum=1)
    if namespace == _MEDIA:
        h264 = _child(configuration, "H264", optional=True)
        if h264 is not None:
            gop = _number(_text(h264, "GovLength"), integer=True)
    elif configuration.get("GovLength") is not None:
        gop = _number(configuration.get("GovLength"), integer=True)
    return {
        "codec": codec,
        "resolution": {
            "width": _number(_text(resolution, "Width"), integer=True, minimum=1, maximum=16384),
            "height": _number(_text(resolution, "Height"), integer=True, minimum=1, maximum=16384),
        },
        "frame_rate": frame,
        "bitrate_kbps": bitrate,
        "encoding_interval": interval,
        "gop": gop,
        "quality": _number(_text(configuration, "Quality")),
    }


def _resolutions(parent):
    result = []
    for node in parent:
        if node.tag != f"{{{_TT}}}ResolutionsAvailable":
            continue
        value = {
            "width": _number(_text(node, "Width"), integer=True, minimum=1, maximum=16384),
            "height": _number(_text(node, "Height"), integer=True, minimum=1, maximum=16384),
        }
        if value in result or len(result) >= 128:
            _error()
        result.append(value)
    return result


def _options(client, endpoint, namespace, profile, current):
    prefix = "trt" if namespace == _MEDIA else "tr2"
    action = "GetVideoEncoderConfigurationOptions"
    raw = _call(
        client,
        endpoint,
        namespace,
        action,
        f"<{prefix}:{action}><{prefix}:ConfigurationToken>{escape(profile.encoder)}"
        f"</{prefix}:ConfigurationToken><{prefix}:ProfileToken>{escape(profile.token)}"
        f"</{prefix}:ProfileToken></{prefix}:{action}>",
    )
    options = {
        "codecs": [],
        "resolutions": [],
        "frame_rate": None,
        "bitrate_kbps": None,
        "gop": None,
        "quality": None,
        "encoding_interval": None,
        "writable_fields": [],
    }
    profiles = []
    if namespace == _MEDIA:
        root = _child(raw, "Options", namespace)
        for name, codec in (("JPEG", "MJPEG"), ("MPEG4", "MPEG4"), ("H264", "H264")):
            if _child(root, name, optional=True) is not None:
                options["codecs"].append(
                    {"codec": codec, "device_supported": True, "product_supported": codec == "H264"}
                )
        h264 = _child(root, "H264", optional=True)
        if h264 is not None:
            options["quality"] = _range(root, "QualityRange")
            options["resolutions"] = _resolutions(h264)
            frame = _range(h264, "FrameRateRange", integer=True, maximum=1000)
            if frame is not None and frame["min"] > 0:
                options["frame_rate"] = {**frame, "values": [], "integer_only": True}
            options["gop"] = _range(h264, "GovLengthRange", integer=True)
            options["encoding_interval"] = _range(h264, "EncodingIntervalRange", integer=True)
            profiles = [
                (n.text or "").strip() for n in h264 if n.tag == f"{{{_TT}}}H264ProfilesSupported"
            ]
            extension = _child(root, "Extension", optional=True)
            if extension is not None:
                extra = _child(extension, "H264", optional=True)
                if extra is not None:
                    options["bitrate_kbps"] = _range(extra, "BitrateRange", integer=True)
    else:
        nodes = [node for node in raw if node.tag == f"{{{namespace}}}Options"]
        if not 1 <= len(nodes) <= 8:
            _error()
        seen = set()
        for node in nodes:
            codec = _codec(_text(node, "Encoding"))
            if codec in seen:
                _error()
            seen.add(codec)
            options["codecs"].append(
                {"codec": codec, "device_supported": True, "product_supported": codec == "H264"}
            )
            if codec != "H264":
                continue
            options["quality"] = _range(node, "QualityRange")
            options["resolutions"] = _resolutions(node)
            options["bitrate_kbps"] = _range(node, "BitrateRange", integer=True)
            rates = (node.get("FrameRatesSupported") or "").split()
            if rates:
                if len(rates) > 128:
                    _error()
                values = [_number(v, minimum=0.001, maximum=1000) for v in rates]
                if len(values) != len(set(values)):
                    _error()
                options["frame_rate"] = {
                    "min": min(values),
                    "max": max(values),
                    "values": values,
                    "integer_only": False,
                }
            lengths = (node.get("GovLengthRange") or "").split()
            if lengths:
                if len(lengths) != 2:
                    _error()
                low, high = [_number(v, integer=True) for v in lengths]
                if low > high:
                    _error()
                options["gop"] = {"min": low, "max": high}
            profiles = (node.get("ProfilesSupported") or "").split()
    if len(profiles) > 16 or any(not _TOKEN.fullmatch(p) for p in profiles):
        _error()
    h264_supported = any(c["codec"] == "H264" for c in options["codecs"])
    if h264_supported:
        fields = ["codec"]
        if options["resolutions"]:
            fields.append("resolution")
        for name in ("frame_rate", "bitrate_kbps", "gop", "quality", "encoding_interval"):
            if options[name] is not None and (
                name not in {"frame_rate", "bitrate_kbps", "encoding_interval"}
                or current[name] is not None
            ):
                fields.append(name)
        options["writable_fields"] = fields
    return options, tuple(profiles), raw


def _roundtrip_safe(configuration):
    # ElementTree retains expanded element/attribute names, but the transport
    # intentionally does not retain input namespace-prefix declarations. A
    # QName in a scalar value therefore cannot be safely rebound, even when
    # its spelling happens to use our usual ``tt`` or ``trt`` prefix.
    qname = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*\Z")
    for node in configuration.iter():
        # ONVIF Name and the configuration referenceToken are schema strings,
        # never QNames; ordinary names such as "North:Entrance" stay writable.
        values = [
            value
            for name, value in node.attrib.items()
            if not (node is configuration and name == "token")
        ]
        if node.tag != f"{{{_TT}}}Name":
            values.append((node.text or "").strip())
        for value in values:
            if qname.fullmatch(value):
                return False
        xsi_type = node.get(f"{{{_XSI}}}type")
        if xsi_type is not None and not qname.fullmatch(xsi_type):
            return False
    return True


def _structure(node):
    return [node.tag, sorted(node.attrib.items()), node.text, [_structure(n) for n in node]]


@dataclass(repr=False)
class VideoSettingsInspection:
    """Private mutable references are released explicitly when the vault closes."""

    source: object = field(repr=False)
    namespace: str = field(repr=False)
    endpoint: str = field(repr=False)
    profile: _Profile = field(repr=False)
    configuration: ET.Element | None = field(repr=False)
    fingerprint: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    codec_profiles: tuple[str, ...] = field(repr=False)
    description: dict = field(repr=False)
    cleared: bool = False

    def public(self):
        if self.cleared:
            _error("video_inspection_expired")
        return _copy(self.description)

    def clear(self):
        self.source = self.configuration = self.profile = None
        self.username = self.password = self.endpoint = self.fingerprint = ""
        self.codec_profiles = ()
        self.description = {}
        self.cleared = True


@dataclass(repr=False)
class VideoSettingsResult:
    inspection: VideoSettingsInspection = field(repr=False)
    source_uri: str = field(repr=False)
    requested: dict = field(repr=False)

    def public(self):
        result = self.inspection.public()
        result.update(requested=_copy(self.requested), configuration_readback_verified=True)
        return result

    def clear(self):
        self.inspection.clear()
        self.source_uri = ""
        self.requested = {}


def _inspect(source, username, password, timeout, *, deadline=None):
    client, device = _camera_client(source, timeout, username=username, password=password)
    if deadline is not None:
        client.deadline = min(client.deadline, deadline)
    selected = None
    saw_profiles = False
    for namespace, endpoint in _media_candidates(client, device):
        try:
            _, profiles = _profiles(client, endpoint, namespace)
        except OnvifError as error:
            if error.code == "unsupported":
                continue
            raise
        saw_profiles = True
        matches = []
        expected = _uri_identity(source.source_uri)
        for profile in profiles:
            try:
                uri = _uri(client, endpoint, namespace, profile)
            except OnvifError as error:
                if error.code == "unsupported":
                    continue
                raise
            if _uri_identity(uri) == expected:
                matches.append(profile)
        if not matches:
            continue
        if len(matches) > 1 and (
            any(p.source is None for p in matches)
            or len({(p.encoder, p.source) for p in matches}) != 1
        ):
            _error("video_profile_ambiguous")
        selected = matches[0]
        break
    if selected is None:
        _error("video_profile_unidentified" if saw_profiles else "video_unsupported")
    configuration = _configuration(client, endpoint, namespace, selected)
    current = _normalize(configuration, namespace)
    reason = None
    try:
        options, codec_profiles, raw_options = _options(
            client, endpoint, namespace, selected, current
        )
    except OnvifError as error:
        if error.code != "unsupported":
            raise
        options = {
            "codecs": [
                {
                    "codec": current["codec"],
                    "device_supported": True,
                    "product_supported": current["codec"] == "H264",
                }
            ],
            "resolutions": [],
            "frame_rate": None,
            "bitrate_kbps": None,
            "gop": None,
            "quality": None,
            "encoding_interval": None,
            "writable_fields": [],
        }
        codec_profiles, raw_options = (), ET.Element("unavailable")
        reason = "video_options_unavailable"
    if not any(c["codec"] == "H264" for c in options["codecs"]):
        reason = reason or "video_codec_unsupported"
    if not _roundtrip_safe(configuration):
        reason = "video_unsafe_extension"
    count = max(
        sum(p.encoder == selected.encoder for p in profiles),
        _number(_text(configuration, "UseCount"), integer=True),
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                namespace,
                endpoint,
                selected.token,
                selected.encoder,
                selected.source,
                sorted((p.token, p.encoder, p.source or "") for p in profiles),
                _structure(configuration),
                _structure(raw_options),
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return VideoSettingsInspection(
        source,
        namespace,
        endpoint,
        selected,
        configuration,
        fingerprint,
        client.username,
        client.password,
        codec_profiles,
        {
            "camera_id": str(source.device.id),
            "status": "available",
            "protocol": "media1" if namespace == _MEDIA else "media2",
            "current": current,
            "options": options,
            "shared_configuration": count > 1,
            "shared_profile_count": count,
            "writable": reason is None,
            "writable_reason": reason,
            "configuration_readback_verified": False,
            "stream_verified": False,
            "observed_fps": None,
        },
    )


def _validate_patch(inspection, patch):
    if not isinstance(inspection, VideoSettingsInspection) or inspection.cleared:
        _error("video_inspection_expired")
    if type(patch) is not dict or not set(patch) <= _FIELDS | {"acknowledge_shared"}:
        _error("video_invalid_request")
    if type(patch.get("acknowledge_shared", False)) is not bool:
        _error("video_invalid_request")
    requested = {k: v for k, v in patch.items() if k != "acknowledge_shared"}
    if not requested:
        _error("video_invalid_request")
    public = inspection.public()
    if not public["writable"]:
        _error("video_unsupported")
    if public["shared_configuration"] and patch.get("acknowledge_shared") is not True:
        _error("video_shared_configuration")
    if requested.get("codec", public["current"]["codec"]) != "H264":
        _error("video_codec_unsupported")
    options = public["options"]
    if not set(requested) <= set(options["writable_fields"]):
        _error("video_invalid_request")
    try:
        for name, value in tuple(requested.items()):
            if name == "codec":
                if type(value) is not str or value != "H264":
                    raise ValueError
            elif name == "resolution":
                if (
                    type(value) is not dict
                    or set(value) != {"width", "height"}
                    or any(type(v) is not int for v in value.values())
                    or value not in options["resolutions"]
                ):
                    raise ValueError
                requested[name] = dict(value)
            else:
                rule = options[name]
                integer = name in {"bitrate_kbps", "gop", "encoding_interval"}
                if type(value) not in {int, float} or (integer and type(value) is not int):
                    raise ValueError
                if not math.isfinite(value) or not rule["min"] <= value <= rule["max"]:
                    raise ValueError
                if name == "frame_rate":
                    if rule["integer_only"] and int(value) != value:
                        raise ValueError
                    if rule["values"] and value not in rule["values"]:
                        raise ValueError
                requested[name] = value
    except (KeyError, TypeError, ValueError, OverflowError):
        _error("video_invalid_request")
    return {**requested, "acknowledge_shared": patch.get("acknowledge_shared", False)}


def _set_text(parent, name, value):
    node = _child(parent, name)
    node.text = str(value)


def _changed_configuration(inspection, patch):
    node = copy.deepcopy(inspection.configuration)
    current = inspection.description["current"]
    changing_codec = patch.get("codec", current["codec"]) != current["codec"]
    if "codec" in patch:
        _set_text(node, "Encoding", "H264")
    if "resolution" in patch:
        resolution = _child(node, "Resolution")
        _set_text(resolution, "Width", patch["resolution"]["width"])
        _set_text(resolution, "Height", patch["resolution"]["height"])
    rate = _child(node, "RateControl", optional=True)
    for field_name, xml_name in (
        ("frame_rate", "FrameRateLimit"),
        ("bitrate_kbps", "BitrateLimit"),
        ("encoding_interval", "EncodingInterval"),
    ):
        if field_name in patch:
            value = patch[field_name]
            if inspection.namespace == _MEDIA and field_name == "frame_rate":
                value = int(value)
            _set_text(rate, xml_name, value)
    if "quality" in patch:
        _set_text(node, "Quality", patch["quality"])
    profiles = inspection.codec_profiles
    if inspection.namespace == _MEDIA:
        h264 = _child(node, "H264", optional=True)
        if changing_codec:
            for child in tuple(node):
                if child.tag in {f"{{{_TT}}}MPEG4", f"{{{_TT}}}H264"}:
                    node.remove(child)
            h264 = ET.Element(f"{{{_TT}}}H264")
            gop = patch.get("gop", current["gop"])
            rule = inspection.description["options"]["gop"]
            if gop is None:
                gop = rule["min"] if rule is not None else None
            if not profiles or gop is None:
                _error("video_unsupported")
            ET.SubElement(h264, f"{{{_TT}}}GovLength").text = str(gop)
            ET.SubElement(h264, f"{{{_TT}}}H264Profile").text = profiles[0]
            index = next(
                (
                    i
                    for i, c in enumerate(node)
                    if c.tag in {f"{{{_TT}}}Multicast", f"{{{_TT}}}SessionTimeout"}
                ),
                len(node),
            )
            node.insert(index, h264)
        if "gop" in patch:
            if h264 is None:
                _error("video_unsupported")
            _set_text(h264, "GovLength", patch["gop"])
    else:
        if "gop" in patch:
            node.set("GovLength", str(patch["gop"]))
        if changing_codec and node.get("Profile") not in profiles:
            if not profiles:
                _error("video_unsupported")
            node.set("Profile", profiles[0])
    node.tag = f"{{{inspection.namespace}}}Configuration"
    return node


def _validate_codec_restore(inspection, desired):
    """Carried fields must fit H.264 as well as explicitly requested fields."""
    if inspection.description["current"]["codec"] == "H264":
        return
    public = inspection.description
    options = public["options"]
    carried = {
        name: desired[name]
        for name in options["writable_fields"]
        if name != "codec" and desired.get(name) is not None
    }
    # Validate every advertised target constraint, without inventing missing
    # capabilities or silently choosing a different rate/resolution/quality.
    _validate_patch(
        inspection,
        {**carried, "codec": "H264", "acknowledge_shared": public["shared_configuration"]},
    )


def _apply(inspection, patch, timeout, before_write):
    deadline = time.monotonic() + timeout
    fresh = _inspect(
        inspection.source, inspection.username, inspection.password, timeout, deadline=deadline
    )
    try:
        if fresh.fingerprint != inspection.fingerprint:
            _error("video_configuration_conflict")
        patch = _validate_patch(fresh, patch)
        configuration = _changed_configuration(fresh, patch)
        # Codec restoration must yield a valid H.264 configuration before writing.
        desired = _normalize(configuration, fresh.namespace)
        if desired["codec"] != "H264":
            _error("video_codec_unsupported")
        _validate_codec_restore(fresh, desired)
        if time.monotonic() + 2 >= deadline:
            _error("video_unreachable")
        client, _ = _camera_client(
            fresh.source,
            max(0.001, deadline - time.monotonic()),
            username=fresh.username,
            password=fresh.password,
        )
        client.deadline = min(client.deadline, deadline)
        prefix = "trt" if fresh.namespace == _MEDIA else "tr2"
        force = (
            "<trt:ForcePersistence>true</trt:ForcePersistence>" if fresh.namespace == _MEDIA else ""
        )
        operation = (
            f"<{prefix}:SetVideoEncoderConfiguration>"
            + ET.tostring(configuration, encoding="unicode")
            + force
            + f"</{prefix}:SetVideoEncoderConfiguration>"
        )
        if before_write is not None:
            before_write(deadline)
        if time.monotonic() >= deadline:
            _error("video_unreachable")
        try:
            _call(
                client, fresh.endpoint, fresh.namespace, "SetVideoEncoderConfiguration", operation
            )
        except OnvifError as error:
            if error.code in {"auth_failed", "unsupported"}:
                raise
            _error("video_outcome_unverified")
        except Exception:
            _error("video_outcome_unverified")
        try:
            readback = _configuration(client, fresh.endpoint, fresh.namespace, fresh.profile)
            current = _normalize(readback, fresh.namespace)
            requested = {k: v for k, v in patch.items() if k != "acknowledge_shared"}
            if any(current[k] != desired[k] for k in requested):
                _error("video_readback_mismatch")
            uri = _stream_account(
                _uri(client, fresh.endpoint, fresh.namespace, fresh.profile), fresh.source
            )
            # ONVIF has no atomic compare-and-set profile operation. Recheck
            # the selected association after obtaining its URI so an external
            # camera-admin reassignment cannot silently persist another channel.
            _, after_profiles = _profiles(client, fresh.endpoint, fresh.namespace)
            if [p for p in after_profiles if p.token == fresh.profile.token] != [fresh.profile]:
                _error("video_outcome_unverified")
            fresh.configuration = readback
            fresh.description["current"] = current
            return VideoSettingsResult(fresh, uri, requested)
        except CameraVideoSettingsError as error:
            if error.code == "video_readback_mismatch":
                raise
            _error("video_outcome_unverified")
        except Exception:
            _error("video_outcome_unverified")
    except BaseException:
        fresh.clear()
        raise


@dataclass(repr=False)
class _CallbackFailure(Exception):
    """Only trusted caller authorization failures cross the worker boundary."""

    error: BaseException = field(repr=False)


def _capture(function, *arguments):
    try:
        return True, function(*arguments)
    except CameraVideoSettingsError as error:
        return False, error.code
    except _CallbackFailure as error:
        return False, error


class OnvifVideoSettingsService:
    """Owned finite workers; cancelling a caller never frees an active slot."""

    def __init__(self, *, connector=None, inspect_seconds=15.0, apply_seconds=20.0):
        if not 0 < inspect_seconds <= 15 or not 0 < apply_seconds <= 20:
            raise ValueError("Video settings budgets refused")
        self.connector = connector or OnvifConnector()
        self.inspect_seconds, self.apply_seconds = inspect_seconds, apply_seconds
        self._closed = False
        self._tasks = set()

    async def _run(self, function, *arguments):
        if self._closed:
            _error("video_closed")
        task = asyncio.create_task(self.connector._run(_capture, function, *arguments))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            try:
                success, result = await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled() and task.exception() is None:
                    completed = task.result()
                    if completed[0] and hasattr(completed[1], "clear"):
                        completed[1].clear()
                raise
        except OnvifError as error:
            raise CameraVideoSettingsError(
                {
                    "auth_failed": "video_auth_failed",
                    "unsupported": "video_unsupported",
                    "busy": "video_busy",
                    "unreachable": "video_unreachable",
                }.get(error.code, "video_invalid_response")
            ) from None
        if not success:
            if isinstance(result, _CallbackFailure):
                raise result.error
            _error(result)
        return result

    async def inspect(self, source, *, username=None, password=None):
        return await self._run(_inspect, source, username, password, self.inspect_seconds)

    def validate_patch(self, inspection, patch):
        if self._closed:
            _error("video_closed")
        return _validate_patch(inspection, patch)

    async def apply(self, inspection, patch, *, before_write=None):
        patch = self.validate_patch(inspection, patch)
        gate = None
        if before_write is not None:
            loop = asyncio.get_running_loop()

            def gate(deadline):
                future = asyncio.run_coroutine_threadsafe(before_write(), loop)
                try:
                    future.result(timeout=max(0.001, deadline - time.monotonic()))
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    _error("video_unreachable")
                except concurrent.futures.CancelledError:
                    raise _CallbackFailure(asyncio.CancelledError()) from None
                except Exception as error:
                    raise _CallbackFailure(error) from None

        return await self._run(_apply, inspection, patch, self.apply_seconds, gate)

    async def aclose(self):
        self._closed = True
        while self._tasks:
            await asyncio.gather(
                *(asyncio.shield(t) for t in tuple(self._tasks)), return_exceptions=True
            )
