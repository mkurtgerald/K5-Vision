"""Real loopback WS-Discovery multicast exchanges, without a socket substitute."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import platform
import socket
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from k5vision.operator_onvif import OnvifConnector

_GROUP, _INTERFACE, _PORT = "239.255.255.250", "127.0.0.1", 3702
_SOAP = "http://www.w3.org/2003/05/soap-envelope"
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_NETWORK = "http://www.onvif.org/ver10/network/wsdl"
_WSDP = "http://schemas.xmlsoap.org/ws/2006/02/devprof"
_NAME, _CAMERA_PORT = "Generated UDP camera", 34567
_CAPABILITY_ERRORS = {
    errno.EACCES,
    errno.EPERM,
    errno.EAFNOSUPPORT,
    errno.EADDRNOTAVAIL,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.ENOPROTOOPT,
    errno.EOPNOTSUPP,
    10013,
    10042,
    10045,
    10047,
    10049,
    10050,
    10051,
    10065,
}


def _text(root, name):
    return next(
        (item.text or "" for item in root.iter() if item.tag.rsplit("}", 1)[-1] == name), ""
    )


def _response(message_id, endpoint, device_type="dn:NetworkVideoTransmitter"):
    types = f"<d:Types>{device_type}</d:Types>" if device_type else ""
    return (
        f'<s:Envelope xmlns:s="{_SOAP}" xmlns:a="{_WSA}" xmlns:d="{_WSD}" '
        f'xmlns:dn="{_NETWORK}" xmlns:wsdp="{_WSDP}">'
        f"<s:Header><a:Action>{_WSD}/ProbeMatches</a:Action>"
        f"<a:RelatesTo>{message_id}</a:RelatesTo></s:Header>"
        "<s:Body><d:ProbeMatches><d:ProbeMatch>"
        "<a:EndpointReference><a:Address>urn:uuid:00000000-0000-0000-0000-000000000001"
        "</a:Address></a:EndpointReference>"
        f"{types}<d:Scopes>onvif://www.onvif.org/name/{quote(_NAME)}</d:Scopes>"
        f"<d:XAddrs>{endpoint}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>"
        "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
    ).encode()


@contextmanager
def _multicast_camera(endpoints, *, malformed_first=False, device_types=None):
    """Join the real loopback multicast group and answer its actual Probe."""
    wire = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        wire.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        wire.bind(("0.0.0.0", _PORT))
        membership = socket.inet_aton(_GROUP) + socket.inet_aton(_INTERFACE)
        wire.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        wire.settimeout(0.05)
    except OSError as error:
        wire.close()
        if (
            error.errno in _CAPABILITY_ERRORS
            or getattr(error, "winerror", None) in _CAPABILITY_ERRORS
        ):
            pytest.skip("Operating system does not provide multicast loopback capability")
        raise
    state = SimpleNamespace(
        received=False,
        action="",
        message_id="",
        requested_types="",
        sent=0,
        errors=[],
        stopped=False,
        closed=False,
    )
    stopping = threading.Event()

    def respond():
        try:
            while not stopping.is_set():
                try:
                    body, sender = wire.recvfrom(65536)
                except TimeoutError:
                    continue
                except OSError:
                    if not stopping.is_set():
                        state.errors.append("socket_receive_failed")
                    return
                if sender[0] != _INTERFACE:
                    continue
                try:
                    root = ET.fromstring(body)
                except ET.ParseError:
                    continue
                action, message_id = _text(root, "Action"), _text(root, "MessageID")
                if action != _WSD + "/Probe" or not message_id.startswith("urn:uuid:"):
                    continue
                state.received, state.action, state.message_id = True, action, message_id
                state.requested_types = _text(root, "Types")
                response_types = (
                    ["dn:NetworkVideoTransmitter"] * len(endpoints)
                    if device_types is None
                    else device_types
                )
                responses = [
                    _response(message_id, endpoint, device_type)
                    for endpoint, device_type in zip(endpoints, response_types, strict=True)
                ]
                if malformed_first:
                    responses.insert(0, b"<incomplete-discovery-packet")
                for response in responses:
                    wire.sendto(response, sender)
                    state.sent += 1
                return
        except Exception:
            state.errors.append("responder_failed")
        finally:
            state.stopped = True

    thread = threading.Thread(target=respond, name="generated-onvif-multicast-camera")
    thread.start()
    try:
        yield state
    finally:
        stopping.set()
        thread.join(timeout=2)
        wire.close()
        state.closed = wire.fileno() == -1
        assert not thread.is_alive(), "Owned multicast camera responder did not stop"
        assert state.stopped and state.closed, "Owned multicast camera socket was not closed"
        assert not state.errors, "Owned multicast camera responder failed"


def test_real_udp_multicast_probe_returns_only_public_camera_metadata(record_testsuite_property):
    endpoint = f"http://{_INTERFACE}:{_CAMERA_PORT}/onvif/device_service"
    with _multicast_camera([endpoint, endpoint], malformed_first=True) as peer:
        candidates = asyncio.run(
            OnvifConnector(discovery_timeout_seconds=0.3).discover(interface=_INTERFACE)
        )
        assert peer.received and peer.action == _WSD + "/Probe"
        assert peer.requested_types == "dn:NetworkVideoTransmitter"
        assert peer.message_id.startswith("urn:uuid:") and peer.sent == 3
        assert candidates == [
            {"host": _INTERFACE, "port": _CAMERA_PORT, "secure": False, "name": _NAME}
        ]
        public = json.dumps(candidates)
        assert endpoint not in public and "onvif/device_service" not in public
        assert "://" not in public and "MessageID" not in public and "Scopes" not in public
    assert peer.stopped and peer.closed
    record_testsuite_property(
        "operator_onvif_udp_receipt",
        json.dumps(
            {
                "schema_version": "1",
                "platform": platform.system(),
                "native_windows_execution": os.name == "nt",
                "actual_multicast_probe_received": True,
                "actual_unicast_response_received": True,
                "malformed_packet_ignored": True,
                "duplicate_endpoint_collapsed": True,
                "public_metadata_only": True,
                "owned_thread_socket_cleanup": True,
            },
            sort_keys=True,
        ),
    )


def test_real_udp_discovery_refuses_hostname_foreign_sender_host_and_credential_urls():
    endpoints = [
        f"http://camera.invalid:{_CAMERA_PORT}/onvif/device_service",
        f"http://127.0.0.2:{_CAMERA_PORT}/onvif/device_service",
        f"http://fixture:secret@{_INTERFACE}:{_CAMERA_PORT}/onvif/device_service",
    ]
    with _multicast_camera(endpoints) as peer:
        candidates = asyncio.run(
            OnvifConnector(discovery_timeout_seconds=0.3).discover(interface=_INTERFACE)
        )
        assert peer.received and peer.sent == 3
        assert candidates == []
    assert peer.stopped and peer.closed


def test_real_udp_discovery_filters_printers_and_requires_exact_onvif_video_type():
    endpoints = [
        f"http://{_INTERFACE}:{_CAMERA_PORT + offset}/wsd/endpoint"
        for offset in range(4)
    ]
    with _multicast_camera(
        endpoints,
        device_types=[
            "wsdp:Device",
            "wsdp:NetworkVideoTransmitter",
            "",
            "dn:NetworkVideoTransmitter",
        ],
    ) as peer:
        candidates = asyncio.run(
            OnvifConnector(discovery_timeout_seconds=0.3).discover(interface=_INTERFACE)
        )
        assert peer.received
        assert peer.requested_types == "dn:NetworkVideoTransmitter"
        assert candidates == [
            {
                "host": _INTERFACE,
                "port": _CAMERA_PORT + 3,
                "secure": False,
                "name": _NAME,
            }
        ]
    assert peer.stopped and peer.closed
